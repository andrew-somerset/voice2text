from __future__ import annotations

import base64
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from voice2text.auth import (
    CurrentUserDpapiProtector,
    DpapiRefreshTokenStore,
    LoopbackCallbackServer,
    OAuthClient,
    OAuthError,
    OAuthMetadata,
    TokenProtectionError,
    build_authorization_request,
    create_pkce_pair,
    discover_oauth_metadata,
    oauth_discovery_url,
    parse_authorization_callback,
    pkce_challenge,
)

SERVER_URL = "https://example-be.glean.com"


def metadata_document() -> dict[str, object]:
    return {
        "issuer": SERVER_URL,
        "authorization_endpoint": f"{SERVER_URL}/oauth/authorize",
        "token_endpoint": f"{SERVER_URL}/oauth/token",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["CHAT", "offline_access"],
    }


def test_pkce_uses_the_rfc_s256_transformation() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    assert pkce_challenge(verifier) == expected
    pair = create_pkce_pair()
    assert len(pair.verifier) == 64
    assert pair.challenge == pkce_challenge(pair.verifier)


def test_metadata_requires_same_origin_s256_and_public_client() -> None:
    document = metadata_document()
    metadata = OAuthMetadata.from_document(document, server_url=SERVER_URL)

    assert metadata.token_endpoint == f"{SERVER_URL}/oauth/token"

    wrong_origin = metadata_document()
    wrong_origin["token_endpoint"] = "https://attacker.invalid/oauth/token"
    with pytest.raises(OAuthError, match="configured HTTPS origin"):
        OAuthMetadata.from_document(wrong_origin, server_url=SERVER_URL)

    no_pkce = metadata_document()
    no_pkce["code_challenge_methods_supported"] = ["plain"]
    with pytest.raises(OAuthError, match="PKCE S256"):
        OAuthMetadata.from_document(no_pkce, server_url=SERVER_URL)

    confidential_only = metadata_document()
    confidential_only["token_endpoint_auth_methods_supported"] = ["client_secret_basic"]
    with pytest.raises(OAuthError, match="public-client"):
        OAuthMetadata.from_document(confidential_only, server_url=SERVER_URL)


def test_metadata_discovery_does_not_follow_redirects_or_expose_body() -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, json=metadata_document())

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    metadata = discover_oauth_metadata(SERVER_URL, client=client)

    assert metadata.issuer == SERVER_URL
    assert str(requested[0].url) == oauth_discovery_url(SERVER_URL)

    failing = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(401, text="sensitive response body")
        )
    )
    with pytest.raises(OAuthError, match="status 401") as error:
        discover_oauth_metadata(SERVER_URL, client=failing)
    assert "sensitive" not in str(error.value)


def test_authorization_request_contains_pkce_and_no_secret() -> None:
    metadata = OAuthMetadata.from_document(metadata_document(), server_url=SERVER_URL)
    request = build_authorization_request(
        metadata,
        client_id="desktop-client",
        redirect_uri="http://127.0.0.1:49152/oauth/callback",
        scopes=("CHAT",),
        state="shared-loopback-state",
    )
    query = parse_qs(urlsplit(request.url).query)

    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["desktop-client"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [pkce_challenge(request.code_verifier)]
    assert query["state"] == ["shared-loopback-state"]
    assert request.state == "shared-loopback-state"
    assert "client_secret" not in query


class MemoryTokenStore:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.saved: list[str] = []
        self.clear_count = 0

    def save(self, refresh_token: str) -> None:
        self.value = refresh_token
        self.saved.append(refresh_token)

    def load(self) -> str | None:
        return self.value

    def clear(self) -> None:
        self.value = None
        self.clear_count += 1


def test_public_client_refresh_sends_no_secret_and_persists_rotated_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=metadata_document())
        return httpx.Response(
            200,
            json={
                "access_token": "memory-only-access",
                "refresh_token": "rotated-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "CHAT offline_access",
            },
        )

    store = MemoryTokenStore("protected-refresh")
    client = OAuthClient(
        server_url=SERVER_URL,
        client_id="desktop-client",
        scopes=("CHAT", "offline_access"),
        token_store=store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: 1_000.0,
    )

    assert client.access_token() == "memory-only-access"
    form = parse_qs(requests[-1].content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["client_id"] == ["desktop-client"]
    assert form["refresh_token"] == ["protected-refresh"]
    assert "client_secret" not in form
    assert store.saved == ["rotated-refresh"]


def test_failed_refresh_is_sanitized_and_clears_stale_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=metadata_document())
        return httpx.Response(401, text="sensitive token-endpoint details")

    store = MemoryTokenStore("stale-refresh")
    client = OAuthClient(
        server_url=SERVER_URL,
        client_id="desktop-client",
        scopes=("CHAT", "offline_access"),
        token_store=store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(OAuthError, match="status 401") as error:
        client.access_token()
    assert "sensitive" not in str(error.value)
    assert store.clear_count == 1


def test_token_request_never_follows_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=metadata_document())
        return httpx.Response(307, headers={"Location": "https://attacker.invalid/token"})

    store = MemoryTokenStore("protected-refresh")
    client = OAuthClient(
        server_url=SERVER_URL,
        client_id="desktop-client",
        scopes=("CHAT", "offline_access"),
        token_store=store,
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ),
    )

    with pytest.raises(OAuthError, match="status 307"):
        client.access_token()

    assert [request.url.host for request in requests] == [
        "example-be.glean.com",
        "example-be.glean.com",
    ]


def test_token_transport_and_json_failures_discard_sensitive_causes() -> None:
    private_marker = "private-refresh-marker"

    def network_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=metadata_document())
        raise httpx.ReadError(private_marker, request=request)

    network_client = OAuthClient(
        server_url=SERVER_URL,
        client_id="desktop-client",
        token_store=MemoryTokenStore(private_marker),
        http_client=httpx.Client(transport=httpx.MockTransport(network_handler)),
    )
    with pytest.raises(OAuthError) as network_error:
        network_client.access_token()
    assert private_marker not in str(network_error.value)
    assert network_error.value.__cause__ is None

    def malformed_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=metadata_document())
        return httpx.Response(200, content=f"not-json-{private_marker}")

    malformed_client = OAuthClient(
        server_url=SERVER_URL,
        client_id="desktop-client",
        token_store=MemoryTokenStore(private_marker),
        http_client=httpx.Client(transport=httpx.MockTransport(malformed_handler)),
    )
    with pytest.raises(OAuthError) as malformed_error:
        malformed_client.access_token()
    assert private_marker not in str(malformed_error.value)
    assert malformed_error.value.__cause__ is None


def test_callback_validates_state_and_rejects_duplicate_parameters() -> None:
    redirect = "http://127.0.0.1:49152/oauth/callback"
    query = urlencode({"code": "short-lived-code", "state": "expected-state"})

    assert (
        parse_authorization_callback(
            f"{redirect}?{query}",
            expected_redirect_uri=redirect,
            expected_state="expected-state",
        )
        == "short-lived-code"
    )
    with pytest.raises(OAuthError, match="state validation"):
        parse_authorization_callback(
            f"{redirect}?code=code&state=wrong",
            expected_redirect_uri=redirect,
            expected_state="expected-state",
        )
    with pytest.raises(OAuthError, match="duplicate"):
        parse_authorization_callback(
            f"{redirect}?code=one&code=two&state=expected-state",
            expected_redirect_uri=redirect,
            expected_state="expected-state",
        )


def test_loopback_server_returns_only_a_validated_code() -> None:
    with LoopbackCallbackServer(expected_state="one-time-state") as callback:
        response = httpx.get(
            f"{callback.redirect_uri}?code=browser-code&state=one-time-state",
            timeout=2.0,
            trust_env=False,
        )
        code = callback.wait_for_code(timeout_seconds=2.0)

    assert response.status_code == 200
    assert "browser-code" not in response.text
    assert code == "browser-code"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows current-user API")
def test_dpapi_round_trip_is_current_user_ciphertext(tmp_path: Path) -> None:
    token = "refresh-token-marker-that-must-not-be-plaintext"
    protector = CurrentUserDpapiProtector()
    protected = protector.protect(token.encode())

    assert token.encode() not in protected
    plaintext = protector.unprotect(protected)
    try:
        assert plaintext.decode() == token
    finally:
        plaintext[:] = b"\0" * len(plaintext)

    path = tmp_path / "refresh.dpapi"
    store = DpapiRefreshTokenStore(path, protector=protector)
    store.save(token)
    assert token.encode() not in path.read_bytes()
    assert store.load() == token
    store.clear()
    assert store.load() is None


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows current-user API")
def test_dpapi_rejects_corrupted_ciphertext() -> None:
    with pytest.raises(TokenProtectionError, match="could not unprotect"):
        CurrentUserDpapiProtector().unprotect(base64.b64decode("bm90LWRwYXBp"))


def test_invalid_decrypted_token_discards_decode_cause_and_zeros_buffer(tmp_path: Path) -> None:
    class InvalidPlaintextProtector:
        def __init__(self) -> None:
            self.plaintext = bytearray(b"\xffprivate-token-marker")

        def protect(self, plaintext: bytes | bytearray) -> bytes:
            return bytes(plaintext)

        def unprotect(self, _protected: bytes) -> bytearray:
            return self.plaintext

    path = tmp_path / "refresh.dpapi"
    path.write_bytes(b"ciphertext-placeholder")
    protector = InvalidPlaintextProtector()
    store = DpapiRefreshTokenStore(path, protector=protector)

    with pytest.raises(TokenProtectionError) as caught:
        store.load()

    assert caught.value.__cause__ is None
    assert protector.plaintext == bytearray(len(protector.plaintext))
