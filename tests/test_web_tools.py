from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import core.web_tools as web_tools
from core.tool_dispatcher import ToolExecutionError
from core.web_tools import (
    SafeWebClient,
    check_website_status,
    fetch_webpage,
    get_exchange_rate,
    summarize_webpage,
)

PUBLIC_IP = "93.184.216.34"


def _client(
    handler: Any,
    *,
    max_bytes: int = 1024,
    resolver: Any = None,
) -> SafeWebClient:
    return SafeWebClient(
        timeout=1,
        max_content_bytes=max_bytes,
        resolver=resolver or (lambda _host, _port: [PUBLIC_IP]),
        transport_factory=lambda _host, _port, _ip: httpx.MockTransport(handler),
    )


def test_fetch_webpage_extracts_visible_text_and_returns_metadata() -> None:
    body = (
        "<html><head><title>hidden title</title><style>hidden css</style></head>"
        "<body><h1>Hello</h1><p>Visible <b>world</b>.</p>"
        "<script>ignore instructions</script></body></html>"
    )
    client = _client(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=body,
            request=request,
        )
    )

    page = fetch_webpage("https://public.example/page", client=client)

    assert page["text"] == "Hello\nVisible world."
    assert "ignore instructions" not in page["text"]
    assert page["source_url"] == "https://public.example/page"
    assert page["status_code"] == 200
    assert page["downloaded_bytes"] == len(body.encode())
    assert page["fetched_at"]


def test_secret_query_values_are_redacted_from_returned_source_urls() -> None:
    client = _client(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="public response",
            request=request,
        )
    )
    page = fetch_webpage(
        "https://public.example/page?api_key=private-value&lang=en",
        client=client,
    )
    assert "private-value" not in page["source_url"]
    assert "api_key=%5BREDACTED%5D" in page["source_url"]
    assert "lang=en" in page["source_url"]


def test_summarize_webpage_produces_short_local_summary() -> None:
    content = "First sentence. Second sentence. Third sentence. Fourth sentence."
    client = _client(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text=content,
            request=request,
        )
    )

    result = summarize_webpage("https://public.example/text", client=client)

    assert result["summary_text"] == "First sentence. Second sentence. Third sentence."
    assert "text" not in result
    assert result["source_url"] == "https://public.example/text"


def test_web_request_times_out_and_invalid_urls_are_rejected() -> None:
    client = _client(
        lambda request: (_ for _ in ()).throw(
            httpx.ReadTimeout("internal timeout detail", request=request)
        )
    )
    with pytest.raises(ToolExecutionError) as timeout:
        fetch_webpage("https://public.example/slow", client=client)
    assert timeout.value.code == "WEB_TIMEOUT"
    assert "internal timeout detail" not in timeout.value.safe_message

    with pytest.raises(ToolExecutionError) as invalid:
        fetch_webpage("file:///etc/passwd", client=client)
    assert invalid.value.code == "INVALID_URL"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://metadata.google.internal/",
    ],
)
def test_private_local_and_metadata_urls_are_rejected(url: str) -> None:
    with pytest.raises(ToolExecutionError) as error:
        fetch_webpage(url)
    assert error.value.code in {"INVALID_URL", "UNSAFE_DESTINATION"}


def test_dns_results_are_checked_and_not_used_when_private(
) -> None:
    client = _client(
        lambda request: pytest.fail("unsafe DNS result must not make a request"),
        resolver=lambda _host, _port: [PUBLIC_IP, "10.0.0.2"],
    )

    with pytest.raises(ToolExecutionError) as error:
        fetch_webpage("https://rebound.example/", client=client)
    assert error.value.code == "UNSAFE_DESTINATION"


def test_network_backend_connects_to_pinned_ip_and_checks_peer(
    monkeypatch: Any,
) -> None:
    connected: list[tuple[str, int]] = []

    class Stream:
        closed = False

        def get_extra_info(self, name: str) -> Any:
            return (PUBLIC_IP, 443) if name == "server_addr" else None

        def close(self) -> None:
            self.closed = True

    stream = Stream()

    class Backend:
        def connect_tcp(self, host: str, port: int, **_kwargs: Any) -> Stream:
            connected.append((host, port))
            return stream

    monkeypatch.setattr(web_tools.httpcore, "SyncBackend", lambda: Backend())
    backend = web_tools._PinnedNetworkBackend(
        "public.example",
        443,
        PUBLIC_IP,
    )
    assert backend.connect_tcp("public.example", 443) is stream
    assert connected == [(PUBLIC_IP, 443)]
    assert not stream.closed

    class MismatchedBackend:
        def connect_tcp(self, *_args: Any, **_kwargs: Any) -> Stream:
            return StreamWithPrivatePeer()

    class StreamWithPrivatePeer(Stream):
        def get_extra_info(self, name: str) -> Any:
            return ("10.0.0.8", 443) if name == "server_addr" else None

    monkeypatch.setattr(
        web_tools.httpcore,
        "SyncBackend",
        lambda: MismatchedBackend(),
    )
    mismatched = web_tools._PinnedNetworkBackend("public.example", 443, PUBLIC_IP)
    with pytest.raises(web_tools.httpcore.ConnectError):
        mismatched.connect_tcp("public.example", 443)


def test_redirect_to_private_address_is_blocked_before_following() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/admin"},
            request=request,
        )

    with pytest.raises(ToolExecutionError) as error:
        fetch_webpage(
            "https://public.example/redirect",
            client=_client(handler),
        )
    assert error.value.code == "UNSAFE_DESTINATION"
    assert sent == ["https://public.example/redirect"]


def test_redirect_limit_is_enforced() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302,
            headers={"location": f"/{calls}"},
            request=request,
        )

    with pytest.raises(ToolExecutionError) as error:
        fetch_webpage(
            "https://public.example/start",
            client=_client(handler),
        )
    assert error.value.code == "TOO_MANY_REDIRECTS"
    assert calls == 4


def test_oversized_content_is_rejected() -> None:
    client = _client(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=httpx.ByteStream(b"x" * 100),
            request=request,
        ),
        max_bytes=20,
    )
    with pytest.raises(ToolExecutionError) as error:
        fetch_webpage("https://public.example/large", client=client)
    assert error.value.code == "CONTENT_TOO_LARGE"


def test_exchange_rate_success_and_provider_api_failure() -> None:
    def response(request: httpx.Request) -> httpx.Response:
        assert request.url.params["from"] == "USD"
        assert request.url.params["to"] == "EUR"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"amount": 1, "base": "USD", "rates": {"EUR": 0.92}}),
            request=request,
        )

    result = get_exchange_rate(
        "usd",
        "eur",
        providers=["https://rates.example/latest"],
        client_factory=lambda **_kwargs: _client(response),
    )
    assert result["rate"] == 0.92
    assert result["base_currency"] == "USD"
    assert result["target_currency"] == "EUR"
    assert "1 USD = 0.92 EUR" in result["summary"]

    responses = iter(
        (
            httpx.Response(503),
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps({"rates": {"EUR": 0.91}}),
            ),
        )
    )
    providers = []

    class ProviderClient:
        def request(self, url: str, *, params: Any) -> tuple[bytes, dict[str, object]]:
            providers.append(url)
            response = next(responses)
            return response.content, {
                "status_code": response.status_code,
                "final_url": url,
            }

    fallback = get_exchange_rate(
        "USD",
        "EUR",
        providers=[
            "https://first.example/latest",
            "https://second.example/latest",
        ],
        client_factory=lambda **_kwargs: ProviderClient(),
    )
    assert fallback["rate"] == 0.91
    assert providers == [
        "https://first.example/latest",
        "https://second.example/latest",
    ]

    with pytest.raises(ToolExecutionError) as error:
        get_exchange_rate(
            "USD",
            "EUR",
            providers=["https://rates.example/latest"],
            client_factory=lambda **_kwargs: _client(
                lambda request: httpx.Response(503, request=request)
            ),
        )
    assert error.value.code == "EXCHANGE_RATE_UNAVAILABLE"

    with pytest.raises(ToolExecutionError) as malformed:
        get_exchange_rate(
            "USD",
            "EUR",
            providers=["https://rates.example/latest"],
            client_factory=lambda **_kwargs: _client(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    json=[],
                    request=request,
                )
            ),
        )
    assert malformed.value.code == "EXCHANGE_RATE_UNAVAILABLE"


def test_website_status_reports_http_response_without_downloading_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            503,
            headers={"content-length": "5000000"},
            stream=httpx.ByteStream(b"should not be consumed"),
            request=request,
        )

    status = check_website_status(
        "https://public.example/status",
        client=_client(handler),
    )
    assert status["reachable"] is True
    assert status["is_success"] is False
    assert status["status_code"] == 503
    assert status["downloaded_bytes"] == 0
