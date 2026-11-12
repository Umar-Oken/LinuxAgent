from collections.abc import Sequence
from pathlib import Path
from threading import Event
from typing import Any

from pydantic import BaseModel, ConfigDict

from core.agent_controller import AgentController
from core.llm_client import (
    ChatMessage,
    LLMResponse,
    ModelCapabilities,
    ToolCall,
)
from core.llm_client import ToolDefinition as LLMToolDefinition
from core.tool_dispatcher import ToolDispatcher, ToolResult
from core.tool_registry import ToolDefinition, ToolRegistry
from security.approval import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalResponse,
    TrustedApprovalInterface,
)
from security.path_guard import PathGuard
from security.permissions import PermissionEngine, PermissionPolicy


class EchoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


class FakeLLMClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        history: Sequence[ChatMessage],
        *,
        system_prompt: str | None = None,
        tools: Sequence[LLMToolDefinition] = (),
    ) -> LLMResponse:
        self.calls.append(
            {
                "history": list(history),
                "system_prompt": system_prompt,
                "tools": list(tools),
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]

    def get_capabilities(self) -> ModelCapabilities:
        raise NotImplementedError


class AllowPermission:
    def is_allowed(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        return True


class DenyPermission:
    def is_allowed(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        return False


class ApproveInterface(TrustedApprovalInterface):
    def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(approval_id=request.approval_id, decision="approve")


class _RecordingMemory:
    def __init__(self) -> None:
        self.started: tuple[str, str] | None = None
        self.tools: list[dict[str, object]] = []
        self.finished: dict[str, object] | None = None

    def start_task(self, task_id: str, user_message: str) -> None:
        self.started = (task_id, user_message)

    def record_tool_execution(
        self,
        task_id: str,
        *,
        tool_name: str,
        status: str,
        error_code: str | None,
        duration_seconds: float,
    ) -> None:
        self.tools.append(
            {
                "task_id": task_id,
                "tool_name": tool_name,
                "status": status,
                "error_code": error_code,
                "duration_seconds": duration_seconds,
            }
        )

    def finish_task(
        self,
        task_id: str,
        *,
        status: str,
        response: str | None,
    ) -> None:
        self.finished = {
            "task_id": task_id,
            "status": status,
            "response": response,
        }


def _response_for_tool(name: str, value: str) -> LLMResponse:
    call = ToolCall(name=name, arguments={"value": value})
    return LLMResponse(
        content="",
        tool_calls=[call],
        assistant_message=ChatMessage(
            role="assistant",
            content="",
            tool_calls=[call],
        ),
    )


def _final(text: str = "Tayyor.") -> LLMResponse:
    return LLMResponse(
        content=text,
        assistant_message=ChatMessage(role="assistant", content=text),
    )


def _setup(
    tmp_path: Path,
    responses: list[object],
    *,
    permission: Any = None,
    permission_level: str = "READ_ONLY",
    max_steps: int = 8,
    max_tool_calls: int = 8,
    max_action_retries: int = 1,
    max_replans: int = 2,
    memory: _RecordingMemory | None = None,
) -> tuple[AgentController, FakeLLMClient, list[str], PathGuard]:
    called: list[str] = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="mock_echo",
            description="Echo a value.",
            category="system",
            argument_schema=EchoArguments.model_json_schema(),
            argument_model=EchoArguments,
            permission_level=permission_level,  # type: ignore[arg-type]
            execution_timeout=1,
            implementation=lambda value: called.append(value) or f"echo:{value}",
        )
    )
    client = FakeLLMClient(responses)
    guard = PathGuard([tmp_path])
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=permission or AllowPermission(),
    )
    controller = AgentController(
        client=client,
        registry=registry,
        dispatcher=dispatcher,
        path_guard=guard,
        system_prompt="test system prompt",
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        max_action_retries=max_action_retries,
        max_replans=max_replans,
        memory=memory,
        output=lambda _text: None,
    )
    return controller, client, called, guard


def test_controller_executes_single_tool_then_returns_final_answer(tmp_path: Path) -> None:
    controller, client, called, guard = _setup(
        tmp_path,
        [_response_for_tool("mock_echo", "one"), _final("Javob")],
    )
    try:
        result = controller.run_task("echo one")
        assert result.status == "completed"
        assert result.response == "Javob"
        assert result.steps == 2
        assert result.tool_calls == 1
        assert called == ["one"]
        assert controller.last_plan is not None
        assert [step.status for step in controller.last_plan.steps] == ["completed"]
        history = client.calls[1]["history"]
        assert history[-1].role == "tool"
        assert "echo:one" in history[-1].content
        assert "UNTRUSTED TOOL DATA" in history[-1].content
    finally:
        guard.close()


def test_controller_persists_task_conversation_and_tool_summary(tmp_path: Path) -> None:
    memory = _RecordingMemory()
    controller, _client, _called, guard = _setup(
        tmp_path,
        [_response_for_tool("mock_echo", "one"), _final("Javob")],
        memory=memory,
    )
    try:
        result = controller.run_task("echo one")
    finally:
        guard.close()

    assert memory.started == (result.task_id, "echo one")
    assert memory.tools[0]["tool_name"] == "mock_echo"
    assert memory.tools[0]["status"] == "success"
    assert memory.finished == {
        "task_id": result.task_id,
        "status": "completed",
        "response": "Javob",
    }


def test_controller_handles_sequential_tool_calls(tmp_path: Path) -> None:
    controller, _client, called, guard = _setup(
        tmp_path,
        [
            _response_for_tool("mock_echo", "one"),
            _response_for_tool("mock_echo", "two"),
            _final(),
        ],
    )
    try:
        result = controller.run_task("echo twice")
        assert result.status == "completed"
        assert result.tool_calls == 2
        assert called == ["one", "two"]
    finally:
        guard.close()


def test_controller_plan_marks_failed_step_and_blocks_dependent_call(
    tmp_path: Path,
) -> None:
    called: list[str] = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="mock_echo",
            description="Echo a value.",
            category="system",
            argument_schema=EchoArguments.model_json_schema(),
            argument_model=EchoArguments,
            permission_level="READ_ONLY",
            execution_timeout=1,
            implementation=lambda value: called.append(value) or value,
        )
    )
    first = ToolCall(name="mock_echo", arguments={"value": "first"})
    second = ToolCall(name="mock_echo", arguments={"value": "second"})
    response = LLMResponse(
        content="",
        tool_calls=[first, second],
        assistant_message=ChatMessage(
            role="assistant",
            content="",
            tool_calls=[first, second],
        ),
    )
    client = FakeLLMClient([response, _final()])
    guard = PathGuard([tmp_path])
    controller = AgentController(
        client=client,
        registry=registry,
        dispatcher=ToolDispatcher(registry, permission_checker=DenyPermission()),
        path_guard=guard,
        system_prompt="test",
        output=lambda _text: None,
    )
    try:
        result = controller.run_task("Run two steps")
    finally:
        guard.close()

    assert result.status == "completed"
    assert called == []
    assert controller.last_plan is not None
    assert [step.status for step in controller.last_plan.steps] == [
        "failed",
        "blocked",
    ]
    assert "DEPENDENCY_FAILED" in client.calls[1]["history"][-1].content


def test_controller_replans_once_after_tool_failure(tmp_path: Path) -> None:
    controller, _client, called, guard = _setup(
        tmp_path,
        [
            _response_for_tool("mock_echo", "first"),
            _response_for_tool("mock_echo", "second"),
            _final(),
        ],
        permission=DenyPermission(),
        max_replans=1,
    )
    try:
        result = controller.run_task("Try alternate step")
    finally:
        guard.close()

    assert result.status == "completed"
    assert called == []
    assert controller.last_plan is not None
    assert controller.last_plan.replan_count == 1


def test_controller_stops_when_replanning_budget_is_exhausted(
    tmp_path: Path,
) -> None:
    controller, client, called, guard = _setup(
        tmp_path,
        [
            _response_for_tool("mock_echo", "first"),
            _response_for_tool("mock_echo", "second"),
            _response_for_tool("mock_echo", "third"),
        ],
        permission=DenyPermission(),
        max_replans=1,
    )
    try:
        result = controller.run_task("Keep trying")
    finally:
        guard.close()

    assert result.status == "retry_limit"
    assert len(client.calls) == 3
    assert called == []


def test_controller_enforces_plan_step_budget(tmp_path: Path) -> None:
    first = ToolCall(name="mock_echo", arguments={"value": "first"})
    second = ToolCall(name="mock_echo", arguments={"value": "second"})
    response = LLMResponse(
        content="",
        tool_calls=[first, second],
        assistant_message=ChatMessage(
            role="assistant",
            content="",
            tool_calls=[first, second],
        ),
    )
    controller, client, called, guard = _setup(
        tmp_path,
        [response],
        max_steps=1,
        max_tool_calls=4,
    )
    try:
        result = controller.run_task("Two steps")
    finally:
        guard.close()

    assert result.status == "step_limit"
    assert result.tool_calls == 0
    assert called == []
    assert len(client.calls) == 1


def test_controller_uses_trusted_approval_before_controlled_tool(tmp_path: Path) -> None:
    controller, _client, called, guard = _setup(
        tmp_path,
        [_response_for_tool("mock_echo", "approved"), _final()],
        permission=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(ApproveInterface()),
        ),
        permission_level="CONTROLLED",
    )
    try:
        result = controller.run_task("echo approved")
        assert result.status == "completed"
        assert called == ["approved"]
    finally:
        guard.close()


def test_denied_tool_result_is_returned_to_model_without_execution(
    tmp_path: Path,
) -> None:
    controller, client, called, guard = _setup(
        tmp_path,
        [_response_for_tool("mock_echo", "denied"), _final()],
        permission=DenyPermission(),
    )
    try:
        result = controller.run_task("try echo")
        assert result.status == "completed"
        assert called == []
        assert "PERMISSION_DENIED" in client.calls[1]["history"][-1].content
    finally:
        guard.close()


def test_malformed_model_response_is_reported_without_dispatch(tmp_path: Path) -> None:
    controller, client, called, guard = _setup(
        tmp_path,
        [{"content": "", "tool_calls": "not-a-list"}],
    )
    try:
        result = controller.run_task("bad response")
        assert result.status == "invalid_response"
        assert result.tool_calls == 0
        assert called == []
        assert len(client.calls) == 1
    finally:
        guard.close()


def test_agent_step_limit_stops_repeated_model_tool_loop(tmp_path: Path) -> None:
    controller, client, called, guard = _setup(
        tmp_path,
        [_response_for_tool("mock_echo", str(index)) for index in range(3)],
        max_steps=2,
        max_tool_calls=10,
    )
    try:
        result = controller.run_task("loop")
        assert result.status == "step_limit"
        assert result.steps == 2
        assert len(client.calls) == 2
        assert called == ["0", "1"]
    finally:
        guard.close()


def test_cancellation_prevents_model_or_tool_execution(tmp_path: Path) -> None:
    controller, client, called, guard = _setup(
        tmp_path,
        [_response_for_tool("mock_echo", "never")],
    )
    cancellation = Event()
    cancellation.set()
    try:
        result = controller.run_task("cancel", cancellation=cancellation)
        assert result.status == "cancelled"
        assert client.calls == []
        assert called == []
    finally:
        guard.close()


def test_final_answer_without_tool_calls(tmp_path: Path) -> None:
    controller, _client, called, guard = _setup(tmp_path, [_final("Salom")])
    try:
        result = controller.run_task("salom")
        assert result.status == "completed"
        assert result.response == "Salom"
        assert result.steps == 1
        assert result.tool_calls == 0
        assert called == []
    finally:
        guard.close()


def test_repeated_failed_operation_is_limited_by_action_retry_budget(
    tmp_path: Path,
) -> None:
    controller, client, called, guard = _setup(
        tmp_path,
        [_response_for_tool("mock_echo", "same")] * 3,
        permission=DenyPermission(),
        max_action_retries=0,
    )
    try:
        result = controller.run_task("repeat denied operation")
        assert result.status == "retry_limit"
        assert len(client.calls) == 2
        assert called == []
    finally:
        guard.close()


def test_controller_does_not_repeat_uncertain_controlled_action(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    call = _response_for_tool("mock_echo", "same")
    controller, _client, called, guard = _setup(
        tmp_path,
        [call, call, _final()],
        permission_level="CONTROLLED",
    )
    dispatch_count = 0

    async def timeout_dispatch(*_args: object, **_kwargs: object) -> ToolResult:
        nonlocal dispatch_count
        dispatch_count += 1
        return ToolResult(
            tool_name="mock_echo",
            status="timeout",
            error_code="TIMEOUT",
            safe_error_message="Vaqt tugadi.",
            execution_duration=1,
        )

    monkeypatch.setattr(controller._dispatcher, "dispatch", timeout_dispatch)
    try:
        result = controller.run_task("Run a controlled action")
        assert result.status == "completed"
        assert dispatch_count == 1
        assert called == []
    finally:
        guard.close()


def test_unexpected_llm_failure_is_reported_without_internal_details(
    tmp_path: Path,
) -> None:
    controller, _client, called, guard = _setup(
        tmp_path,
        [RuntimeError("private endpoint and credential detail")],
    )
    messages: list[str] = []
    controller._output = messages.append

    try:
        result = controller.run_task("service unavailable")
        assert result.status == "llm_error"
        assert called == []
        assert messages == ["XATO: Kutilmagan xatolik yuz berdi."]
    finally:
        guard.close()


def test_cancellation_during_tool_waits_for_current_call_then_stops(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    controller, client, called, guard = _setup(
        tmp_path,
        [_response_for_tool("mock_echo", "one"), _final()],
    )
    cancellation = Event()
    original_execute = controller._dispatcher._execute

    async def execute(definition: ToolDefinition, arguments: dict[str, Any]) -> Any:
        result = await original_execute(definition, arguments)
        cancellation.set()
        return result

    monkeypatch.setattr(controller._dispatcher, "_execute", execute)

    try:
        result = controller.run_task("cancel during tool", cancellation=cancellation)
        assert result.status == "cancelled"
        assert result.tool_calls == 1
        assert called == ["one"]
        assert len(client.calls) == 1
    finally:
        guard.close()


def test_tool_completion_after_deadline_stops_before_next_model_call(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    now = [0.0]
    controller, client, called, guard = _setup(
        tmp_path,
        [_response_for_tool("mock_echo", "one"), _final()],
    )
    controller._max_task_seconds = 1
    controller._clock = lambda: now[0]
    original_execute = controller._dispatcher._execute

    async def slow(definition: ToolDefinition, arguments: dict[str, Any]) -> Any:
        result = await original_execute(definition, arguments)
        now[0] = 2.0
        return result

    monkeypatch.setattr(controller._dispatcher, "_execute", slow)

    try:
        result = controller.run_task("slow tool")
        assert result.status == "time_limit"
        assert result.tool_calls == 1
        assert called == ["one"]
        assert len(client.calls) == 1
    finally:
        guard.close()
