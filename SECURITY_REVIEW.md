# Security review

Review date: 2026-11-13. Scope: the terminal agent, registered-tool
dispatcher, filesystem guard, web client, approval policy, audit log and
workflow engine.

## Findings and mitigations

| Area | Result | Evidence / mitigation |
| --- | --- | --- |
| File and web prompt injection | Mitigated | Tool results are inserted as `UNTRUSTED TOOL DATA`; the system prompt says that file, web and document content is data, not instructions. `test_security_adversarial.py` verifies the execution constraints are not mutated. |
| Traversal, symlinks and races | Mitigated | `PathGuard` is default-deny, uses canonical authorization plus descriptor-relative `O_NOFOLLOW` opens. Existing path, archive, document and developer-tool tests cover traversal, symlink swaps and archive links; the new adversarial test covers a sensitive directory and escape symlink. |
| Unauthorized tools and approval bypass | Mitigated | `ToolRegistry` rejects unknown/disabled tools; `ToolDispatcher` validates schema then invokes `PermissionEngine`. Controlled calls use argument-bound, single-use approvals which are rechecked immediately before execution. |
| SSRF and redirects | Mitigated | `SafeWebClient` permits only public HTTP(S), pins DNS to a verified peer address, disallows non-standard ports and revalidates redirects. `tests/test_web_tools.py` covers private-address redirects and redirect limits. |
| Resource exhaustion | Mitigated | Tool timeouts, response-size limits, document byte/result/time limits, archive limits, request body limits and bounded agent/workflow steps are enforced. |
| Malformed tool calls | Mitigated | Pydantic and JSON Schema validation returns safe stable errors before authorization/execution. The new adversarial test covers malformed arguments and an unregistered name. |
| Secret leakage and logging | Mitigated | Audit arguments redact secret keys, content and paths; tool results are not written to audit events. Audit logs use private permissions, rotation, retention and symlink rejection. |
| Infinite loops | Mitigated | Agent limits steps, tool calls, retries, replans and task time; workflows bound steps, calls and duration. |

## Default configuration review

The default filesystem root list is empty (deny by default). Permission policy
is `writes`, so `CONTROLLED` and `SENSITIVE` tools need a trusted approval
interface. The dispatcher itself defaults to deny. Production deployments
should keep allowed roots narrow, leave system/sensitive paths disabled, and
use a dedicated audit-log data directory.

## Test evidence

`tests/test_security_adversarial.py` adds cross-boundary adversarial checks.
Existing focused suites cover path guarding, web networking, approval, audit,
documents, archives, dispatcher, controller limits and configuration.

## Remaining limitations

Prompt injection cannot be eliminated when untrusted text must be shown to an
LLM; it remains a model-behaviour risk and is mitigated by framing, bounded
context and the independent tool/permission boundary. A synchronous tool or
model call cannot be forcibly interrupted by Python; its configured timeout
bounds it, and cancellation is observed when the call returns. Directory
renames by a privileged external process cannot be fully constrained without
Linux `openat2`-style kernel resolution.
