"""Bounded public-web requests with DNS-pinned SSRF protection."""

from __future__ import annotations

import ipaddress
import json
import math
import socket
import ssl
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpcore
import httpx

from core.tool_dispatcher import ToolExecutionError

_MAX_REDIRECTS = 3
_MAX_TEXT_CHARS = 3_000
_MAX_METADATA_URL_BYTES = 384
_BLOCKED_HOSTS = {
    "metadata",
    "metadata.google.internal",
    "metadata.azure.internal",
    "instance-data",
    "instance-data.ec2.internal",
}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DROP_CONTENT_TAGS = {
    "head",
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "template",
}
Resolver = Callable[[str, int], list[str]]
TransportFactory = Callable[[str, int, str], httpx.BaseTransport]
_SECRET_QUERY_KEYS = (
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "token",
    "access_token",
    "refresh_token",
    "client_secret",
    "password",
    "signature",
    "sig",
)


def _canonical_host(host: str) -> str:
    return host.rstrip(".").encode("idna").decode("ascii").casefold()


def _redact_url(url: str) -> tuple[str, bool]:
    parsed = urlsplit(url)
    safe_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.casefold().replace("-", "_")
        is_secret = (
            normalized_key in _SECRET_QUERY_KEYS
            or normalized_key.endswith(("_key", "_token", "_secret", "_password", "_signature"))
            or normalized_key.endswith("_credential")
            or normalized_key == "key"
        )
        safe_query.append((key, "[REDACTED]" if is_secret else value))
    safe_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(safe_query), "")
    )
    encoded = safe_url.encode("utf-8")
    if len(encoded) > _MAX_METADATA_URL_BYTES:
        safe_url = (
            encoded[: _MAX_METADATA_URL_BYTES - 3].decode("utf-8", errors="ignore")
            + "..."
        )
        return safe_url, True
    return safe_url, False


def _validate_url(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise ToolExecutionError("INVALID_URL", "URL noto'g'ri yoki juda uzun.")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError
        host = _canonical_host(hostname)
        scheme = parsed.scheme.casefold()
        default_port = 443 if scheme == "https" else 80
        effective_port = port or default_port
        if effective_port != default_port or _is_blocked_hostname(host):
            raise ValueError
        try:
            literal_address = ipaddress.ip_address(host)
        except ValueError:
            literal_address = None
        if literal_address is not None:
            _validate_public_ip(str(literal_address))
        # Ensure malformed authority forms are rejected before handing them to HTTPX.
        normalized = urlunsplit(
            (
                parsed.scheme.casefold(),
                parsed.netloc,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        httpx.URL(normalized)
    except (UnicodeError, ValueError, httpx.InvalidURL):
        raise ToolExecutionError(
            "INVALID_URL",
            "Faqat to'g'ri HTTP(S) URL manzillaridan foydalanish mumkin.",
        ) from None
    return normalized, host, effective_port


def _is_blocked_hostname(host: str) -> bool:
    return (
        host in _BLOCKED_HOSTS
        or host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
    )


def _validate_public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        raise ToolExecutionError(
            "UNSAFE_DESTINATION",
            "URL domeni xavfsiz ochiq IP manzilga yechilmagan.",
        ) from None
    if not address.is_global or address.is_multicast or address.is_unspecified:
        raise ToolExecutionError(
            "UNSAFE_DESTINATION",
            "Mahalliy, xususiy yoki maxsus IP manzillarga so'rov taqiqlangan.",
        )
    return str(address)


def _resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return [_validate_public_ip(str(literal))]
    try:
        records = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except (OSError, socket.gaierror):
        raise ToolExecutionError(
            "DNS_RESOLUTION_FAILED",
            "URL domen nomini aniqlab bo'lmadi.",
        ) from None
    addresses = list(
        dict.fromkeys(_validate_public_ip(record[4][0]) for record in records)
    )
    if not addresses:
        raise ToolExecutionError(
            "DNS_RESOLUTION_FAILED",
            "URL domeni uchun IP manzil topilmadi.",
        )
    return addresses


class _PinnedNetworkBackend(httpcore.SyncBackend):
    def __init__(self, host: str, port: int, address: str) -> None:
        self._host = host
        self._port = port
        self._address = address
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        if _canonical_host(host) != self._host or port != self._port:
            raise httpcore.ConnectError("Unexpected network destination")
        stream = self._backend.connect_tcp(
            self._address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        peer = stream.get_extra_info("server_addr")
        try:
            peer_ip = _validate_public_ip(str(peer[0]))
        except (IndexError, TypeError, ToolExecutionError):
            stream.close()
            raise httpcore.ConnectError("Could not verify connected address") from None
        if peer_ip != self._address:
            stream.close()
            raise httpcore.ConnectError("Connected address did not match DNS validation")
        return stream


class _PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(self, host: str, port: int, address: str) -> None:
        super().__init__(trust_env=False, http1=True, http2=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
            network_backend=_PinnedNetworkBackend(host, port, address),
        )


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed: list[str] = []
        self._blocks = {
            "address",
            "article",
            "blockquote",
            "br",
            "dd",
            "div",
            "dl",
            "dt",
            "fieldset",
            "figcaption",
            "figure",
            "footer",
            "form",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "section",
            "table",
            "tr",
            "ul",
        }

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._suppressed.append(tag)
        elif not self._suppressed and tag in self._blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._suppressed:
            if tag == self._suppressed[-1]:
                self._suppressed.pop()
            return
        if tag in self._blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed and data.strip():
            if (
                self.parts
                and self.parts[-1]
                and self.parts[-1][-1].isalnum()
                and data[0].isalnum()
            ):
                self.parts.append(" ")
            self.parts.append(data)


class SafeWebClient:
    """Fetch public HTTP(S) resources with bounded bodies and pinned DNS."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_content_bytes: int = 1_000_000,
        resolver: Resolver = _resolve_public_addresses,
        transport_factory: TransportFactory = _PinnedHTTPTransport,
    ) -> None:
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 3.0))
        self._max_content_bytes = max_content_bytes
        self._resolver = resolver
        self._transport_factory = transport_factory

    def request(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        read_body: bool = True,
    ) -> tuple[bytes, dict[str, object]]:
        if params:
            url = str(httpx.URL(url).copy_merge_params(params))
        current_url = url
        source_url: str | None = None
        redirects = 0
        for redirect_number in range(_MAX_REDIRECTS + 1):
            normalized_url, host, port = _validate_url(current_url)
            if source_url is None:
                source_url = normalized_url
            addresses = self._resolver(host, port)
            if not addresses:
                raise ToolExecutionError(
                    "DNS_RESOLUTION_FAILED",
                    "URL domeni uchun IP manzil topilmadi.",
                )
            # Validate injected/custom resolver results too, before pinning the socket.
            safe_addresses = [_validate_public_ip(address) for address in addresses]
            transport = self._transport_factory(host, port, safe_addresses[0])
            try:
                with httpx.Client(
                    transport=transport,
                    follow_redirects=False,
                    timeout=self._timeout,
                    trust_env=False,
                    headers={
                        "Accept-Encoding": "identity",
                        "User-Agent": "LinuxAgent/1.0 (+public-web-tools)",
                    },
                ) as client:
                    with client.stream("GET", normalized_url) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location:
                                return b"", self._metadata(
                                    source_url,
                                    normalized_url,
                                    response.status_code,
                                    response.headers,
                                    0,
                                    redirects,
                                )
                            if redirect_number >= _MAX_REDIRECTS:
                                raise ToolExecutionError(
                                    "TOO_MANY_REDIRECTS",
                                    "Veb-manzil ruxsat etilgan redirect limitidan oshdi.",
                                )
                            current_url = urljoin(normalized_url, location)
                            redirects += 1
                            continue
                        encoding = response.headers.get("content-encoding", "identity")
                        if encoding.casefold() not in {"", "identity"}:
                            raise ToolExecutionError(
                                "UNSUPPORTED_CONTENT_ENCODING",
                                "Siqilgan javoblar xavfsiz tekshirish uchun qabul qilinmaydi.",
                            )
                        if read_body:
                            length = response.headers.get("content-length")
                            if length is not None:
                                try:
                                    declared_length = int(length)
                                except ValueError:
                                    raise ToolExecutionError(
                                        "INVALID_RESPONSE",
                                        "Server javobining hajm metama'lumoti noto'g'ri.",
                                    ) from None
                                if declared_length < 0 or declared_length > self._max_content_bytes:
                                    raise ToolExecutionError(
                                        "CONTENT_TOO_LARGE",
                                        "Veb-javob ruxsat etilgan hajm limitidan oshdi.",
                                    )
                            if response.is_stream_consumed:
                                body = response.content
                                if len(body) > self._max_content_bytes:
                                    raise ToolExecutionError(
                                        "CONTENT_TOO_LARGE",
                                        "Veb-javob ruxsat etilgan hajm limitidan oshdi.",
                                    )
                            else:
                                chunks: list[bytes] = []
                                total = 0
                                for chunk in response.iter_raw():
                                    total += len(chunk)
                                    if total > self._max_content_bytes:
                                        raise ToolExecutionError(
                                            "CONTENT_TOO_LARGE",
                                            "Veb-javob ruxsat etilgan hajm limitidan oshdi.",
                                        )
                                    chunks.append(chunk)
                                body = b"".join(chunks)
                        else:
                            body = b""
                        return body, self._metadata(
                            source_url,
                            str(response.url),
                            response.status_code,
                            response.headers,
                            len(body),
                            redirects,
                        )
            except ToolExecutionError:
                raise
            except httpx.TimeoutException as exc:
                raise ToolExecutionError(
                    "WEB_TIMEOUT",
                    "Veb-so'rov uchun vaqt limiti tugadi.",
                ) from exc
            except (httpx.HTTPError, OSError, ssl.SSLError) as exc:
                raise ToolExecutionError(
                    "WEB_REQUEST_FAILED",
                    "Veb-manzilga xavfsiz ulanish yoki javobni olish imkoni bo'lmadi.",
                ) from exc
        raise ToolExecutionError("TOO_MANY_REDIRECTS", "Redirect limiti oshib ketdi.")

    @staticmethod
    def _metadata(
        source_url: str,
        final_url: str,
        status_code: int,
        headers: httpx.Headers,
        downloaded_bytes: int,
        redirects: int,
    ) -> dict[str, object]:
        safe_source_url, source_url_truncated = _redact_url(source_url)
        safe_final_url, final_url_truncated = _redact_url(final_url)
        return {
            "source_url": safe_source_url,
            "final_url": safe_final_url,
            "source_url_truncated": source_url_truncated,
            "final_url_truncated": final_url_truncated,
            "status_code": status_code,
            "content_type": headers.get("content-type", "application/octet-stream"),
            "content_length": headers.get("content-length"),
            "downloaded_bytes": downloaded_bytes,
            "redirects": redirects,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }


def _decode_text(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for parameter in content_type.split(";")[1:]:
        name, separator, value = parameter.partition("=")
        if separator and name.strip().casefold() == "charset":
            charset = value.strip().strip("\"'")[:40]
            break
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _extract_readable_text(body: bytes, content_type: str) -> tuple[str, bool]:
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
        raise ToolExecutionError(
            "UNSUPPORTED_CONTENT_TYPE",
            "Faqat HTML yoki oddiy matnli veb-sahifalarni o'qish mumkin.",
        )
    decoded = _decode_text(body, content_type)
    if media_type == "text/plain":
        visible = decoded
    else:
        parser = _VisibleTextParser()
        parser.feed(decoded)
        parser.close()
        visible = "".join(parser.parts)
    cleaned = "\n".join(line.strip() for line in visible.splitlines() if line.strip())
    cleaned = "".join(
        character
        for character in cleaned
        if character in "\n\t" or ord(character) >= 32
    )
    truncated = len(cleaned) > _MAX_TEXT_CHARS
    return cleaned[:_MAX_TEXT_CHARS], truncated


def fetch_webpage(
    url: str,
    *,
    timeout: float = 10.0,
    max_content_bytes: int = 1_000_000,
    client: SafeWebClient | None = None,
) -> dict[str, object]:
    web_client = client or SafeWebClient(
        timeout=timeout,
        max_content_bytes=max_content_bytes,
    )
    body, metadata = web_client.request(url)
    text, truncated = _extract_readable_text(
        body,
        str(metadata["content_type"]),
    )
    return {
        **metadata,
        "text": text,
        "text_truncated": truncated,
        "summary": (
            f"Veb-sahifadan {metadata['downloaded_bytes']} bayt olindi; "
            f"{len(text)} ta matn belgisi ajratildi."
        ),
    }


def summarize_webpage(
    url: str,
    *,
    timeout: float = 10.0,
    max_content_bytes: int = 1_000_000,
    client: SafeWebClient | None = None,
) -> dict[str, object]:
    page = fetch_webpage(
        url,
        timeout=timeout,
        max_content_bytes=max_content_bytes,
        client=client,
    )
    text = str(page["text"])
    sentences = [
        sentence.strip()
        for sentence in text.replace("\n", " ").split(". ")
        if sentence.strip()
    ]
    summary = ". ".join(sentences[:3])
    if summary and not summary.endswith((".", "!", "?")):
        summary += "."
    if not summary:
        summary = text[:800]
    return {
        **{key: value for key, value in page.items() if key != "text"},
        "summary_text": summary,
    }


def get_exchange_rate(
    base_currency: str,
    target_currency: str,
    *,
    providers: Iterable[str],
    timeout: float = 10.0,
    max_content_bytes: int = 1_000_000,
    client_factory: Callable[..., SafeWebClient] = SafeWebClient,
) -> dict[str, object]:
    base = base_currency.upper()
    target = target_currency.upper()
    errors = 0
    for provider_url in providers:
        try:
            client = client_factory(
                timeout=timeout,
                max_content_bytes=max_content_bytes,
            )
            body, metadata = client.request(
                provider_url,
                params={"from": base, "to": target},
            )
            if not 200 <= int(metadata["status_code"]) < 300:
                errors += 1
                continue
            payload = json.loads(body)
            if not isinstance(payload, dict):
                errors += 1
                continue
            rates = payload.get("rates")
            rate: object = rates.get(target) if isinstance(rates, dict) else None
            if rate is None and isinstance(payload.get("conversion_rate"), (int, float)):
                rate = payload["conversion_rate"]
            if rate is None and isinstance(payload.get("conversion_rates"), dict):
                rate = payload["conversion_rates"].get(target)
            if (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not math.isfinite(float(rate))
                or float(rate) <= 0
            ):
                errors += 1
                continue
            return {
                **metadata,
                "base_currency": base,
                "target_currency": target,
                "rate": float(rate),
                "provider": urlsplit(str(metadata["final_url"])).hostname,
                "summary": f"1 {base} = {float(rate):.6g} {target}.",
            }
        except (ToolExecutionError, ValueError, TypeError, json.JSONDecodeError):
            errors += 1
    if not errors:
        raise ToolExecutionError(
            "NO_EXCHANGE_RATE_PROVIDER",
            "Valyuta kursi provayderi sozlanmagan.",
        )
    raise ToolExecutionError(
        "EXCHANGE_RATE_UNAVAILABLE",
        "Valyuta kursini ishonchli provayderlardan olib bo'lmadi.",
    )


def check_website_status(
    url: str,
    *,
    timeout: float = 10.0,
    client: SafeWebClient | None = None,
) -> dict[str, object]:
    web_client = client or SafeWebClient(
        timeout=timeout,
        max_content_bytes=1024,
    )
    _body, metadata = web_client.request(url, read_body=False)
    status_code = int(metadata["status_code"])
    return {
        **metadata,
        "reachable": True,
        "is_success": 200 <= status_code < 400,
        "summary": (
            f"Veb-sayt javob berdi: HTTP {status_code}."
            if status_code < 400
            else f"Veb-sayt HTTP {status_code} holatini qaytardi."
        ),
    }
