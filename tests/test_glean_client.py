from __future__ import annotations

import json
import threading
from collections.abc import Callable

import httpx
import pytest

from voice2text.glean_client import (
    Citation,
    GleanAccessDeniedError,
    GleanAuthenticationError,
    GleanClientError,
    GleanProtocolError,
    GleanQueuedError,
    GleanRateLimitError,
    GleanRequestError,
    GleanTimeoutError,
    LiveGleanClient,
    MockGleanClient,
)


class _TokenProvider:
    def __init__(self, *tokens: str) -> None:
        self._tokens = tokens or ("test-access-token",)
        self._index = 0
        self.access_calls = 0
        self.invalidations = 0

    def access_token(self) -> str:
        self.access_calls += 1
        return self._tokens[min(self._index, len(self._tokens) - 1)]

    def invalidate_access_token(self) -> None:
        self.invalidations += 1
        self._index += 1


def _stream_response(*documents: object) -> httpx.Response:
    body = b"\n".join(json.dumps(document).encode("utf-8") for document in documents) + b"\n"
    return httpx.Response(200, content=body, headers={"Content-Type": "text/plain"})


def _run_live_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    token_provider: _TokenProvider | None = None,
    query: str = "final local transcript",
) -> tuple[object, ...]:
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = LiveGleanClient(
            server_url="https://gm-be.glean.com",
            token_provider=token_provider or _TokenProvider(),
            http_client=http_client,
            request_timeout_seconds=30,
        )
        return tuple(client.stream_answer(query))


def test_mock_stream_is_complete_and_network_free() -> None:
    chunks = tuple(
        MockGleanClient(delay_seconds=0, sleep=lambda _seconds: None).stream_answer(
            "a local test query"
        )
    )

    assert "".join(chunk.text_delta for chunk in chunks).startswith("This is a simulated")
    assert chunks[-1].done is True
    assert chunks[-1].citations[0].title == "Mock source — no company data"
    assert chunks[-1].citations[0].url.startswith("https://example.invalid/")


def test_mock_does_not_echo_query_into_answer() -> None:
    secret_marker = "do-not-echo-this-marker"

    answer = "".join(
        chunk.text_delta for chunk in MockGleanClient(delay_seconds=0).stream_answer(secret_marker)
    )

    assert secret_marker not in answer


def test_mock_stream_honors_cancellation() -> None:
    cancel = threading.Event()

    def cancel_during_first_delay(_seconds: float) -> None:
        cancel.set()

    chunks = tuple(
        MockGleanClient(sleep=cancel_during_first_delay).stream_answer(
            "cancel this query",
            cancel_event=cancel,
        )
    )

    assert chunks == ()


@pytest.mark.parametrize("query", ["", "   ", "invalid\0query"])
def test_invalid_query_is_rejected(query: str) -> None:
    with pytest.raises(ValueError):
        tuple(MockGleanClient(delay_seconds=0).stream_answer(query))


def test_mock_can_surface_sanitized_failure() -> None:
    failure = GleanClientError("simulated policy denial")

    with pytest.raises(GleanClientError, match="policy denial"):
        tuple(MockGleanClient(failure=failure).stream_answer("safe query"))


def test_citation_requires_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        Citation(title="Invalid", url="http://example.test")

    with pytest.raises(ValueError, match="HTTPS"):
        Citation(title="Invalid", url="https://user:password@example.test/source")


def test_live_stream_uses_real_chat_path_and_parses_answer_citations() -> None:
    captured: dict[str, object] = {}
    document_citation = {
        "sourceDocument": {
            "id": "document-1",
            "title": "Company Handbook",
            "url": "https://glean.example.test/handbook",
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content)
        return _stream_response(
            {
                "messages": [
                    {
                        "author": "GLEAN_AI",
                        "messageType": "UPDATE",
                        "fragments": [{"text": "private progress text"}],
                    },
                    {
                        "author": "GLEAN_AI",
                        "messageType": "WARNING",
                        "fragments": [{"text": "private warning text"}],
                    },
                    {
                        "author": "GLEAN_AI",
                        "messageType": "CONTENT",
                        "fragments": [
                            {"text": "The answer ", "citation": document_citation},
                            {
                                "citation": {
                                    "sourceFile": {
                                        "name": "Policy.pdf",
                                        "url": "https://glean.example.test/policy.pdf",
                                    }
                                }
                            },
                            {
                                "citation": {
                                    "sourcePerson": {
                                        "name": "Ada Lovelace",
                                        "metadata": {
                                            "externalProfileLink": (
                                                "https://glean.example.test/people/ada"
                                            )
                                        },
                                    }
                                }
                            },
                            {
                                "citation": {
                                    "sourceCustomEntity": {
                                        "title": "Factory Alpha",
                                        "metadata": {
                                            "url": "https://glean.example.test/factories/alpha"
                                        },
                                    }
                                }
                            },
                        ],
                        "citations": [document_citation],
                    },
                ]
            },
            {
                "messages": [
                    {
                        "author": "GLEAN_AI",
                        "messageType": "CONTENT",
                        "fragments": [{"text": "continues."}],
                    },
                    {"author": "GLEAN_AI", "messageType": "CONTROL_FINISH"},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = LiveGleanClient(
            server_url="https://gm-be.glean.com",
            token_provider=_TokenProvider(),
            application_id="approved-app-id",
            save_chat=False,
            http_client=http_client,
        )
        chunks = tuple(client.stream_answer("  final local transcript  "))

    assert captured == {
        "method": "POST",
        "path": "/rest/api/v1/chat",
        "url": "https://gm-be.glean.com/rest/api/v1/chat",
        "authorization": "Bearer test-access-token",
        "body": {
            "messages": [
                {
                    "author": "USER",
                    "messageType": "CONTENT",
                    "fragments": [{"text": "final local transcript"}],
                }
            ],
            "saveChat": False,
            "stream": True,
            "timeoutMillis": 30000,
            "applicationId": "approved-app-id",
        },
    }
    assert "".join(chunk.text_delta for chunk in chunks) == "The answer continues."
    assert chunks[-1].done is True
    assert [(citation.title, citation.url) for citation in chunks[-1].citations] == [
        ("Company Handbook", "https://glean.example.test/handbook"),
        ("Policy.pdf", "https://glean.example.test/policy.pdf"),
        ("Ada Lovelace", "https://glean.example.test/people/ada"),
        ("Factory Alpha", "https://glean.example.test/factories/alpha"),
    ]
    assert "private progress text" not in "".join(chunk.text_delta for chunk in chunks)
    assert "private warning text" not in "".join(chunk.text_delta for chunk in chunks)


def test_live_stream_refreshes_once_after_401() -> None:
    provider = _TokenProvider("expired-access-token", "refreshed-access-token")
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["Authorization"])
        if len(authorizations) == 1:
            return httpx.Response(401, text="private response body")
        return _stream_response(
            {
                "messages": [
                    {
                        "author": "GLEAN_AI",
                        "messageType": "CONTENT",
                        "fragments": [{"text": "Refreshed."}],
                    }
                ]
            }
        )

    chunks = _run_live_client(handler, token_provider=provider)

    assert authorizations == [
        "Bearer expired-access-token",
        "Bearer refreshed-access-token",
    ]
    assert provider.access_calls == 2
    assert provider.invalidations == 1
    assert "".join(chunk.text_delta for chunk in chunks) == "Refreshed."
    assert chunks[-1].done is True


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (202, GleanQueuedError),
        (400, GleanRequestError),
        (401, GleanAuthenticationError),
        (403, GleanAccessDeniedError),
        (408, GleanTimeoutError),
        (429, GleanRateLimitError),
        (500, GleanClientError),
    ],
)
def test_live_http_failures_are_mapped_without_response_content(
    status_code: int,
    expected_error: type[GleanClientError],
) -> None:
    private_marker = "private-response-marker"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=private_marker)

    with pytest.raises(expected_error) as caught:
        _run_live_client(handler)

    assert private_marker not in str(caught.value)


def test_live_protocol_and_server_errors_do_not_echo_response_content() -> None:
    private_marker = "private-response-marker"

    def malformed_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=f"not-json-{private_marker}\n")

    with pytest.raises(GleanProtocolError) as malformed:
        _run_live_client(malformed_handler)
    assert private_marker not in str(malformed.value)
    assert malformed.value.__cause__ is None

    def server_error_handler(_request: httpx.Request) -> httpx.Response:
        return _stream_response(
            {
                "messages": [
                    {
                        "author": "GLEAN_AI",
                        "messageType": "ERROR",
                        "fragments": [{"text": private_marker}],
                    }
                ]
            }
        )

    with pytest.raises(GleanClientError) as server_error:
        _run_live_client(server_error_handler)
    assert private_marker not in str(server_error.value)


def test_live_network_failure_does_not_retain_token_bearing_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("private-network-marker", request=request)

    with pytest.raises(GleanClientError) as caught:
        _run_live_client(handler)

    assert "private-network-marker" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_live_stream_rejects_oversized_response_line() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))

    with pytest.raises(GleanProtocolError, match="line exceeded"):
        _run_live_client(handler)


def test_live_stream_honors_precancel_without_request_or_token() -> None:
    provider = _TokenProvider()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _stream_response({"messages": []})

    cancel = threading.Event()
    cancel.set()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = LiveGleanClient(
            server_url="https://gm-be.glean.com",
            token_provider=provider,
            http_client=http_client,
        )
        chunks = tuple(client.stream_answer("do not send", cancel_event=cancel))

    assert chunks == ()
    assert requests == 0
    assert provider.access_calls == 0


def test_live_token_provider_failure_is_sanitized() -> None:
    class FailingTokenProvider(_TokenProvider):
        def access_token(self) -> str:
            raise RuntimeError("private-token-marker")

    with pytest.raises(GleanAuthenticationError) as caught:
        _run_live_client(
            lambda _request: _stream_response({"messages": []}),
            token_provider=FailingTokenProvider(),
        )

    assert "private-token-marker" not in str(caught.value)
