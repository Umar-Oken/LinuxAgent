# Architecture and Repository Audit

## Current repository

The application is a single-file terminal agent. `agent.py` contains the CLI
loop, Ollama request handling, prompt and response parsing, all six tools, and
tool dispatch. `README.md` documents setup and the existing user-facing features.
Configuration is defined in `core/config.py` and supports optional YAML and
environment overrides. `requirements.txt` lists runtime and test dependencies.
Tests currently cover configuration behavior; the original agent tools do not
yet have a broader regression suite.

### Existing behavior

The interactive CLI accepts a task and sends it with a fixed system prompt
through `OllamaClient`. The adapter normalizes text, native tool-call, and
structured JSON fallback responses. The agent runs at most eight tool steps
per task, appends each tool result to the conversation, and prints a short
result preview. It handles exit words, EOF, and Ctrl+C.

The six registered tools are:

| Tool | Existing behavior |
|---|---|
| `find_files` | Searches a supplied directory tree for filename fragments or extensions, skipping hidden directories and limiting results to 20. |
| `read_text` | Reads up to 6,000 characters from a `.txt` or `.md` path. |
| `save_note` | Displays note content and destination, asks for the exact response `ha`, then writes under `~/agent_notes/`. |
| `calculate` | Parses and evaluates a restricted arithmetic AST without using `eval`, with expression, depth, and integer-size limits. |
| `system_status` | Reports CPU, memory, and root disk usage using `psutil`. |
| `get_weather` | Looks up a city and current conditions through the Open-Meteo geocoding and forecast endpoints. |

Ollama is called through the `OllamaClient` adapter and `ollama` Python
package. Its model, host, timeout, and bounded retry count come from validated
configuration. Defaults preserve the existing model and use the local Ollama
endpoint. `psutil`, `ollama`, Pydantic, PyYAML, JSON Schema, HTTPX, and pytest
are declared in `requirements.txt`.

## Audit findings

- **Tight coupling:** CLI input/output, model calls, prompt construction,
  parsing, dispatch, and tool implementations share one module and use global
  state. This makes isolated testing and replacing dependencies harder.
- **Configuration enforcement boundary:** `core/config.py` validates
  application settings, YAML input, environment overrides, and path shape.
  Security policies, tool categories, and agent limits are not yet enforced by
  tool execution; they remain configuration data until a later task wires them
  into execution.
- **Tool boundary:** Dispatch selects from a fixed dictionary, so model output
  cannot choose an arbitrary Python function or shell command. However,
  arguments are passed directly to tool functions without a shared typed
  schema or centralized authorization policy.
- **Filesystem scope:** `find_files` and `read_text` accept caller-supplied
  paths without configured allowed roots. `read_text` limits extensions and
  content length, and `save_note` asks for confirmation, but paths and writes
  are not governed by a common security boundary.
- **Untrusted results:** Tool output is appended to the model conversation.
  Files and external API responses can contain untrusted text; there is no
  explicit result trust boundary or audit log.
- **Capability fallback:** The adapter checks model metadata before native tool
  use. If native tool calling is not advertised, it uses structured JSON
  output; there is no automatic model switch.
- **Network and failure handling:** Weather requests use `urllib` with a
  per-request timeout. Ollama errors are caught broadly in the request loop;
  tool execution also catches broad exceptions. The model client and network
  clients are not injected.
- **Persistence and tests:** Conversation history exists only for a task.
  SQLite is not currently used. No test suite is tracked, so tool and agent
  behavior currently lack automated regression coverage.
- **No significant duplicated implementation** was identified in the current
  single-file code; the larger concern is separation of responsibilities.

These are observations about the current implementation, not changes to its
behavior. In particular, this task does not add path restrictions, alter model
configuration, or change tool execution.

## Proposed module responsibilities

The directories below are package scaffolding. Configuration now lives in
`core/config.py`; the rest of the implementation remains in `agent.py`; `core/llm_client.py` now defines the
model interface and Ollama adapter used by the agent loop. `core/tool_registry.py`
provides an explicit, schema-validated registry, but the existing six tools
have not yet been migrated to it.

| Directory | Intended responsibility |
|---|---|
| `core/` | Agent orchestration, typed configuration, shared domain types, and model-independent interfaces such as `LLMClient`. |
| `tools/` | Individual tool implementations; registry definitions are supplied explicitly and do not auto-import modules. Keep side effects behind explicit interfaces. |
| `security/` | Tool argument validation, authorization and allowed-path policies, approval handling, and execution limits. |
| `interfaces/` | Terminal presentation and input handling; future interfaces can use the core without adding GUI or speech processing now. |
| `storage/` | Persistence adapters, including a future SQLite implementation for history and audit records. |
| `tests/` | Unit and integration tests using temporary directories, fake model clients, and mocked external services. |

## Dependency direction

The intended dependency flow is from outer adapters toward the core:

```text
interfaces/  ->  core/  <-  tools/
                    ^
                    |
            security/ and storage/
```

The core orchestration depends on the `LLMClient` interface rather than the
concrete Ollama SDK. The Ollama adapter implements the interface and
normalizes native tool calls or schema-constrained JSON responses. Tool implementations may use
security-approved capabilities and return data, but should not invoke the
agent loop. The interface layer should call the core and render results; it
should not own tool policy. Avoid circular imports. The exact module APIs will
be defined when implementation is migrated in a later task.

## Safe tool dispatch

`core/tool_dispatcher.py` accepts a structured tool call, resolves it only
through `ToolRegistry`, validates it against the registered JSON Schema and
Pydantic argument model (which must forbid extra fields), then asks an injected permission checker before
calling the implementation. The default checker denies every call. Results use
a normalized `ToolResult`; exception details are not returned, and serialized
output is bounded by the dispatcher limit.

Async implementations are awaited with cancellation on timeout. Python cannot
safely terminate an arbitrary synchronous function running in the same process;
the dispatcher intentionally does not send such functions to worker threads
that could continue running after a timeout. It detects a synchronous timeout
only after the callable returns. A synchronous implementation that blocks can
therefore block its caller; strict deadlines require cooperative async tools
or a separately isolated process design.

The current six tools have not been migrated and `agent.py` still uses its
legacy inline dispatch path. This dispatcher therefore does not yet mediate
those legacy calls; migration must route them through the registry and an
explicit permission checker before it replaces that path.

## Tool execution lifecycle

The target lifecycle is:

1. Receive a user task through an interface and create a bounded task context.
2. Ask the configured model for a response through an injected model client.
3. Parse and validate the response shape; reject malformed or unknown tool
   requests without executing them.
4. Validate tool arguments against the selected tool's schema.
5. Apply authorization, approval requirements, and per-task execution limits
   before invoking a tool.
6. Execute only registered application functions; never execute model-generated
   shell commands or Python code.
7. Treat tool results and external content as untrusted data, record the
   appropriate audit information, and return a bounded result to the model.
8. Continue until a validated final answer is produced or a configured limit
   is reached, then render the outcome through the interface.

The current implementation is simpler: it parses model JSON, looks up the
function in `TOOLS`, calls it with parsed keyword arguments, adds its returned
text to the conversation, and stops after a final response or eight tool
steps. The target lifecycle is descriptive and is not implemented by this
scaffold.

## Migration strategy

1. Keep `agent.py` as the working entry point and establish the package layout
   without moving or rewriting behavior.
2. Add tests around existing tools, response parsing, step limits, and CLI
   exit handling before extracting implementations.
3. Introduce typed configuration and model/tool interfaces, preserving current
   defaults and command-line behavior.
4. Migrate tools incrementally into the explicit registry and then centralize
   authorization; add path-scope and approval tests before changing filesystem
   behavior. The registry exists, but existing tool functions are not yet
   registered through it.
5. Move orchestration and terminal handling into `core/` and `interfaces/`,
   retaining a compatibility entry point until parity is established.
6. Add storage only when a later requirement defines persistence behavior;
   SQLite is not added as part of this preparation.

Each migration step should preserve existing functionality and be tested
before proceeding. This task does not advance beyond the initial scaffold.
