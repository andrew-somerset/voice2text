"""Mockable Glean answer streaming with network-free and OAuth-backed clients."""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from voice2text.auth import normalize_server_url

_CHAT_PATH = "/rest/api/v1/chat"
_MAX_QUERY_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_LINE_BYTES = 2 * 1024 * 1024


class GleanClientError(RuntimeError):
    """Base error for a Glean request without exposing response content."""


class GleanAuthenticationError(GleanClientError):
    """The employee must sign in again before using Ask Glean."""


class GleanAccessDeniedError(GleanClientError):
    """Glean denied the signed-in employee access to the requested operation."""


class GleanRequestError(GleanClientError):
    """Glean rejected the request without exposing its response body."""


class GleanTimeoutError(GleanClientError):
    """The Glean request exceeded a client or server timeout."""


class GleanRateLimitError(GleanClientError):
    """Glean temporarily rate-limited the signed-in employee."""


class GleanQueuedError(GleanClientError):
    """Glean queued the request behind another request for the same chat."""


class GleanProtocolError(GleanClientError):
    """Glean returned a malformed or unexpectedly large stream."""


class AccessTokenProvider(Protocol):
    """Small OAuth surface required by the live Chat client."""

    def access_token(self) -> str: ...

    def invalidate_access_token(self) -> None: ...


def _is_safe_https_url(url: str) -> bool:
    if (
        not isinstance(url, str)
        or len(url) > 16_384
        or any(character.isspace() for character in url)
    ):
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return False
    return (
        parts.scheme.lower() == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
    )


@dataclass(frozen=True, slots=True)
class Citation:
    """One source reference rendered separately from answer text."""

    title: str
    url: str

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("citation title cannot be empty")
        if not _is_safe_https_url(self.url):
            raise ValueError("citation URL must use HTTPS")


@dataclass(frozen=True, slots=True)
class GleanChunk:
    """One incremental answer update from a mock or live client."""

    text_delta: str = ""
    citations: tuple[Citation, ...] = ()
    done: bool = False

    def __post_init__(self) -> None:
        if not self.text_delta and not self.citations and not self.done:
            raise ValueError("a Glean chunk must contain an update or mark completion")


class GleanClient(Protocol):
    """Synchronous streaming surface consumed only by the Glean worker thread."""

    def stream_answer(
        self,
        query: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[GleanChunk]: ...


_DEFAULT_CHUNKS = (
    "This is a simulated Ask Glean response. ",
    "No company data was searched, and no query left this computer. ",
    "Live results remain disabled until GM approves the OAuth integration.",
)
_DEFAULT_CITATION = Citation(
    title="Mock source — no company data",
    url="https://example.invalid/voice2text/mock-source",
)


class MockGleanClient:
    """Stream deterministic fake content without opening a network connection."""

    def __init__(
        self,
        *,
        chunks: tuple[str, ...] = _DEFAULT_CHUNKS,
        citations: tuple[Citation, ...] = (_DEFAULT_CITATION,),
        delay_seconds: float = 0.08,
        sleep: Callable[[float], None] = time.sleep,
        failure: GleanClientError | None = None,
    ) -> None:
        if not chunks or any(not chunk for chunk in chunks):
            raise ValueError("mock answer chunks cannot be empty")
        if delay_seconds < 0:
            raise ValueError("mock delay cannot be negative")
        self._chunks = chunks
        self._citations = citations
        self._delay_seconds = delay_seconds
        self._sleep = sleep
        self._failure = failure

    def stream_answer(
        self,
        query: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[GleanChunk]:
        """Yield fake chunks while deliberately ignoring the query content."""

        _validate_query(query)
        if self._failure is not None:
            raise self._failure

        for text_delta in self._chunks:
            if cancel_event is not None and cancel_event.is_set():
                return
            self._sleep(self._delay_seconds)
            if cancel_event is not None and cancel_event.is_set():
                return
            yield GleanChunk(text_delta=text_delta)

        if cancel_event is None or not cancel_event.is_set():
            yield GleanChunk(citations=self._citations, done=True)


class LiveGleanClient:
    """Stream Glean Client Chat responses using an employee OAuth access token."""

    def __init__(
        self,
        *,
        server_url: str,
        token_provider: AccessTokenProvider,
        application_id: str | None = None,
        save_chat: bool = False,
        request_timeout_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not 1.0 <= request_timeout_seconds <= 120.0:
            raise ValueError("Glean request timeout must be between 1 and 120 seconds")
        if application_id is not None:
            _validate_plain_value(application_id, label="application ID")
        self._server_url = normalize_server_url(server_url)
        self._token_provider = token_provider
        self._application_id = application_id
        self._save_chat = save_chat
        self._request_timeout_seconds = request_timeout_seconds
        self._own_http_client = http_client is None
        self._http_client = http_client or httpx.Client(
            follow_redirects=False,
            timeout=request_timeout_seconds,
        )

    def stream_answer(
        self,
        query: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[GleanChunk]:
        """Send only the final query text and yield answer deltas plus deduplicated citations."""

        normalized_query = _validate_query(query)
        if cancel_event is not None and cancel_event.is_set():
            return

        payload: dict[str, Any] = {
            "messages": [
                {
                    "author": "USER",
                    "messageType": "CONTENT",
                    "fragments": [{"text": normalized_query}],
                }
            ],
            "saveChat": self._save_chat,
            "stream": True,
            "timeoutMillis": int(self._request_timeout_seconds * 1_000),
        }
        if self._application_id is not None:
            payload["applicationId"] = self._application_id

        citations: list[Citation] = []
        citation_urls: set[str] = set()
        for attempt in range(2):
            if cancel_event is not None and cancel_event.is_set():
                return
            access_token = self._get_access_token()
            if cancel_event is not None and cancel_event.is_set():
                return
            try:
                with self._http_client.stream(
                    "POST",
                    f"{self._server_url}{_CHAT_PATH}",
                    json=payload,
                    headers={
                        "Accept": "application/x-ndjson, application/json, text/plain",
                        "Authorization": f"Bearer {access_token}",
                    },
                    follow_redirects=False,
                    timeout=self._request_timeout_seconds,
                ) as response:
                    if response.status_code == 401 and attempt == 0:
                        self._invalidate_access_token()
                        continue
                    _raise_for_status(response.status_code)

                    for document in _iter_response_documents(response):
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        text_deltas, response_citations = _parse_chat_response(document)
                        for citation in response_citations:
                            if citation.url not in citation_urls:
                                citation_urls.add(citation.url)
                                citations.append(citation)
                        for text_delta in text_deltas:
                            if cancel_event is not None and cancel_event.is_set():
                                return
                            yield GleanChunk(text_delta=text_delta)
            except httpx.TimeoutException:
                raise GleanTimeoutError("Glean did not respond before the timeout") from None
            except httpx.HTTPError:
                raise GleanClientError("Could not reach Glean") from None
            else:
                if cancel_event is None or not cancel_event.is_set():
                    yield GleanChunk(citations=tuple(citations), done=True)
                return

        raise GleanAuthenticationError("Glean sign-in has expired")

    def close(self) -> None:
        """Close only an internally owned HTTP client."""

        if self._own_http_client:
            self._http_client.close()

    def __enter__(self) -> LiveGleanClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _get_access_token(self) -> str:
        try:
            access_token = self._token_provider.access_token()
        except Exception:
            raise GleanAuthenticationError("Glean sign-in is required") from None
        _validate_plain_value(access_token, label="access token", secret=True)
        return access_token

    def _invalidate_access_token(self) -> None:
        try:
            self._token_provider.invalidate_access_token()
        except Exception:
            raise GleanAuthenticationError("Could not refresh Glean sign-in") from None


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise TypeError("Glean query must be text")
    normalized = query.strip()
    if not normalized:
        raise ValueError("Glean query cannot be empty")
    if "\0" in query:
        raise ValueError("Glean query cannot contain a NUL character")
    if len(normalized.encode("utf-8")) > _MAX_QUERY_BYTES:
        raise ValueError("Glean query exceeds the size limit")
    return normalized


def _validate_plain_value(value: str, *, label: str, secret: bool = False) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64 * 1024
        or any(ord(character) < 0x20 for character in value)
    ):
        if secret:
            raise GleanAuthenticationError(f"Glean {label} is invalid")
        raise ValueError(f"Glean {label} is invalid")


def _raise_for_status(status_code: int) -> None:
    if status_code == 200:
        return
    if status_code == 202:
        raise GleanQueuedError("Glean queued the request; try again after it finishes")
    if status_code == 400:
        raise GleanRequestError("Glean rejected the request")
    if status_code == 401:
        raise GleanAuthenticationError("Glean sign-in has expired")
    if status_code == 403:
        raise GleanAccessDeniedError("Glean denied access for the signed-in employee")
    if status_code == 408:
        raise GleanTimeoutError("Glean did not respond before the timeout")
    if status_code == 429:
        raise GleanRateLimitError("Glean is temporarily rate limiting requests")
    raise GleanClientError(f"Glean request failed with status {status_code}")


def _iter_response_documents(response: httpx.Response) -> Iterator[object]:
    buffer = bytearray()
    total_bytes = 0
    for chunk in response.iter_bytes():
        total_bytes += len(chunk)
        if total_bytes > _MAX_RESPONSE_BYTES:
            raise GleanProtocolError("Glean response exceeded the size limit")
        buffer.extend(chunk)
        while True:
            newline_index = buffer.find(b"\n")
            if newline_index < 0:
                break
            line = bytes(buffer[:newline_index]).rstrip(b"\r")
            del buffer[: newline_index + 1]
            if line:
                yield _decode_response_line(line)
        if len(buffer) > _MAX_RESPONSE_LINE_BYTES:
            raise GleanProtocolError("Glean response line exceeded the size limit")
    if buffer:
        yield _decode_response_line(bytes(buffer).rstrip(b"\r"))


def _decode_response_line(line: bytes) -> object:
    if len(line) > _MAX_RESPONSE_LINE_BYTES:
        raise GleanProtocolError("Glean response line exceeded the size limit")
    try:
        return json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise GleanProtocolError("Glean returned a malformed response stream") from None


def _parse_chat_response(document: object) -> tuple[tuple[str, ...], tuple[Citation, ...]]:
    if not isinstance(document, dict):
        raise GleanProtocolError("Glean response was not a JSON object")
    messages = document.get("messages", [])
    if messages is None:
        messages = []
    if not isinstance(messages, list):
        raise GleanProtocolError("Glean response messages were malformed")

    text_deltas: list[str] = []
    citations: list[Citation] = []
    for message in messages:
        if not isinstance(message, dict):
            raise GleanProtocolError("Glean response contained a malformed message")
        message_type = message.get("messageType", "CONTENT")
        if not isinstance(message_type, str):
            raise GleanProtocolError("Glean response contained an invalid message type")
        if message_type == "ERROR":
            raise GleanClientError("Glean could not complete the request")
        if message_type == "CONTROL_CANCEL":
            raise GleanClientError("Glean cancelled the response")
        if message_type == "CONTROL_RETRY":
            raise GleanClientError("Glean requested that the operation be retried")
        if message.get("author") != "GLEAN_AI" or message_type != "CONTENT":
            continue

        fragments = message.get("fragments", [])
        if fragments is None:
            fragments = []
        if not isinstance(fragments, list):
            raise GleanProtocolError("Glean response fragments were malformed")
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise GleanProtocolError("Glean response contained a malformed fragment")
            text = fragment.get("text")
            if text is not None and not isinstance(text, str):
                raise GleanProtocolError("Glean response contained invalid answer text")
            if text:
                text_deltas.append(text)
            citation = _parse_citation(fragment.get("citation"))
            if citation is not None:
                citations.append(citation)

        legacy_citations = message.get("citations", [])
        if isinstance(legacy_citations, list):
            for raw_citation in legacy_citations:
                citation = _parse_citation(raw_citation)
                if citation is not None:
                    citations.append(citation)
    return tuple(text_deltas), tuple(citations)


def _parse_citation(document: object) -> Citation | None:
    if not isinstance(document, dict):
        return None

    title: str | None = None
    url: str | None = None
    source_fields = (
        ("sourceDocument", ("title", "name"), "Document source"),
        ("sourceFile", ("name", "title"), "File source"),
        ("sourcePerson", ("name", "preferredName"), "Person source"),
        ("sourceCustomEntity", ("title", "name"), "Glean source"),
    )
    for field, title_fields, fallback_title in source_fields:
        source = document.get(field)
        if not isinstance(source, dict):
            continue
        title = _first_text(source, title_fields) or title or fallback_title
        url = _first_https_url(source) or url
        if url:
            return Citation(title=title, url=url)

    reference_document = _reference_document(document)
    if reference_document is not None:
        title = _first_text(reference_document, ("title", "name")) or title or "Glean source"
        url = _first_https_url(reference_document) or url
    else:
        title = title or _first_text(document, ("title", "name")) or "Glean source"
        url = url or _first_https_url(document)
    return Citation(title=title, url=url) if url else None


def _reference_document(citation: dict[str, Any]) -> dict[str, Any] | None:
    ranges = citation.get("referenceRanges")
    if not isinstance(ranges, list):
        return None
    for reference_range in ranges:
        if not isinstance(reference_range, dict):
            continue
        text_range = reference_range.get("textRange")
        if not isinstance(text_range, dict):
            continue
        document = text_range.get("document")
        if isinstance(document, dict):
            return document
    return None


def _first_text(document: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = document.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = document.get("metadata")
    if isinstance(metadata, dict):
        for field in fields:
            value = metadata.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _first_https_url(document: dict[str, Any]) -> str | None:
    fields = ("url", "profileUrl", "externalProfileLink", "link")
    for field in fields:
        value = document.get(field)
        if isinstance(value, str) and _is_safe_https_url(value):
            return value
    metadata = document.get("metadata")
    if isinstance(metadata, dict):
        for field in fields:
            value = metadata.get(field)
            if isinstance(value, str) and _is_safe_https_url(value):
                return value
    return None


def main(argv: list[str] | None = None) -> int:
    """Print a network-free mock stream for explicit manual inspection."""

    parser = argparse.ArgumentParser(description="Test the network-free mock Glean stream")
    parser.add_argument("--query", default="How does the mock work?")
    args = parser.parse_args(argv)

    for chunk in MockGleanClient().stream_answer(args.query):
        if chunk.text_delta:
            print(chunk.text_delta, end="", flush=True)
        if chunk.done:
            print(f"\nCitations: {len(chunk.citations)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
