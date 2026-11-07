from security.error_handler import ApplicationErrorHandler
from security.path_guard import PathAccessError


def test_known_application_errors_use_user_safe_messages() -> None:
    handler = ApplicationErrorHandler()
    error = type("LLMTransportError", (Exception,), {})("private endpoint details")

    message = handler.user_message(error)

    assert "Ollama" in message
    assert "private endpoint details" not in message


def test_path_policy_error_is_explained_without_internal_details() -> None:
    handler = ApplicationErrorHandler()

    message = handler.user_message(PathAccessError("DENIED", "/secret/private"))

    assert "rad etdi" in message
    assert "/secret/private" not in message


def test_unknown_error_does_not_expose_exception_details() -> None:
    message = ApplicationErrorHandler().user_message(
        RuntimeError("traceback, credentials, and internal path")
    )

    assert message == "Kutilmagan xatolik yuz berdi."
    assert "credentials" not in message
