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
  Allowed roots and enabled categories are enforced when constructing the
  runtime registry and path guard. Agent step count remains bounded by the
  existing loop limit.
- **Tool boundary:** All six existing tools are selected from the explicit
  registry; dispatcher validation and authorization are required before
  implementations run. There is no model-generated code or command execution.
- **Filesystem scope:** File search, reads, and note writes use `PathGuard`.
  File operations are denied unless the user configures explicit allowed roots;
  no home-directory root is granted automatically.
- **Untrusted results:** Tool data is size-bounded, internal absolute paths are
  redacted, and results are marked as untrusted in model history. No audit log
  is persisted yet.
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

This audit describes the repository before the incremental architecture tasks.
The six-tool dispatch and filesystem findings below have since been addressed
by the registry, dispatcher, and path-guard integration. SQLite persistence and
the extraction of CLI orchestration remain future work.

## Proposed module responsibilities

Configuration lives in `core/config.py`; `core/llm_client.py` defines the
model interface and Ollama adapter; and `core/tool_registry.py` holds the
explicit schemas and implementations for the six existing tools. The working
CLI entry point and tool implementations remain in `agent.py`. The dispatcher
and path policy are in `core/tool_dispatcher.py` and `security/path_guard.py`.

| Directory | Intended responsibility |
|---|---|
| `core/` | Typed configuration, LLM adapter, explicit tool registry, validation, authorization, dispatch, and current agent orchestration entry point. |
| `tools/` | Future location for extracted implementations; no auto-import or discovery is used. |
| `security/` | Allowed-path policy and symlink-resistant file access. |
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

The agent loop depends on the `LLMClient` interface rather than the concrete
Ollama SDK. The Ollama adapter normalizes native tool calls or schema-validated
JSON responses. The registry contains only explicitly supplied callables;
dispatch validates, authorizes, and executes them. File wrappers use
`PathGuard` for actual reads and writes. Tool implementations return data and
do not invoke the agent loop. The interface layer should render results but
not own tool policy.

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

All six existing tools are registered with Pydantic argument models,
categories, permission levels, timeouts, and explicit implementations. The
agent advertises only enabled registry schemas and routes calls through the
dispatcher. `security/permissions.py` evaluates the registered tool identity,
validated arguments, target resource, permission level, and configured
`approval_policy`. Read-only calls are allowed under `writes`; controlled and
sensitive calls require approval; prohibited calls are always denied.
`security/approval.py` accepts decisions only from an injected trusted
interface. Approval requests display the action, target, impact, and ID; grants
are bound to the exact tool and arguments, expire, and are consumed once.
Dispatcher authorization is rechecked immediately before execution. Without a
trusted approval interface, actions requiring approval fail closed.
`save_note` also preserves its existing `ha` confirmation before writing.

## Audit logging and user-safe errors

`security/audit_logger.py` writes structured JSON Lines to the XDG application
data directory (`personal-ai-agent/audit.jsonl`). Audit records include UTC
time, per-process session ID, task ID, tool, sanitized arguments, permission
decision, status, duration, and a stable error code. Values associated with
credentials are redacted; document/text values and local paths are omitted.
Tool result bodies, exception messages, and tracebacks are never logged.

The audit directory is restricted to the current user, files are created with
mode `0600`, and logs rotate at 5 MiB with five backups. Configured logging
retention removes expired active/rotated files. Approval and tool events are
written through their respective managers. If approval logging fails, the
approval fails closed. If logging fails after a tool call, the result reports
that the tool may have completed so the user can check effects before retrying.
`security/error_handler.py` maps known application errors to safe terminal
messages without including exception details.

## Filesystem access guard

`security/path_guard.py` grants access only to roots explicitly passed to
`PathGuard`; it never defaults to the home directory. SSH, credential, browser
profile, and system locations are denied even when nested under an allowed
root, unless `allow_sensitive=True` is deliberately configured (and the path
must still be under an allowed root). Existing paths are required by default;
missing write targets must be explicitly permitted with `allow_missing=True`.
Use `open_file` for actual reads/writes: it traverses directory descriptors
with no-follow semantics and returns an owned file descriptor, while
`authorize_path` only validates a location and cannot pin it against a later
filesystem change. Symlinks are rejected by descriptor-relative opens, even
when their target remains within an allowed root.

Portable Python cannot prevent another privileged process from renaming an
already-open directory outside the allowed tree. The guard avoids detached
worker operations and symlink-based TOCTOU races but documents this residual
directory-rename limitation; stronger Linux-only confinement would require
`openat2` with beneath/no-symlink resolution.

## Tool execution lifecycle

The intended lifecycle is:

1. Receive a user task through an interface and create a bounded task context.
2. Ask the configured model for a response through an injected model client.
3. Parse and validate the response shape; reject malformed or unknown tool
   requests without executing them.
4. Validate tool arguments against the selected tool's schema.
5. Apply authorization, approval requirements, and per-task execution limits
   before invoking a tool.
6. Execute only registered application functions; never execute model-generated
   shell commands or Python code.
7. Treat tool results and external content as untrusted data, record redacted
   audit metadata, and return a bounded result to the model.
8. Continue until a validated final answer is produced or a configured limit
   is reached, then render the outcome through the interface.

The current implementation follows this lifecycle through step 7: it parses
model responses through the adapter, advertises enabled registry schemas,
dispatches and validates calls, enforces permission and time limits, and adds
sanitized results as explicitly untrusted conversation data. It stops after a
final response or eight tool steps and renders through the existing terminal
loop. Configurable persistence, audit records, and extraction of orchestration
from `agent.py` are not implemented.

## Migration strategy

1. Keep `agent.py` as the working entry point while preserving behavior.
2. Introduce typed configuration and the Ollama adapter.
3. Register the six existing tools, route execution through the dispatcher,
   and enforce explicit filesystem roots and approvals.
4. Move orchestration and terminal handling into `core/` and `interfaces/`,
   retaining a compatibility entry point until parity is established.
5. Add storage only when a later requirement defines persistence behavior;
   SQLite is not added as part of this preparation.

Steps 1-3 are implemented. The remaining items are future work and are not
started automatically.
