"""OAuth PKCE and current-user token protection for the optional Glean route."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

_PKCE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
_PKCE_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_SCOPE_PATTERN = re.compile(r"^[!#-\[\]-~]+$")
_DISCOVERY_PATH = "/.well-known/oauth-authorization-server"
_CALLBACK_PATH = "/oauth/callback"
_MAX_CALLBACK_URL_LENGTH = 16_384
_MAX_METADATA_BYTES = 64 * 1024
_MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
_MAX_PROTECTED_TOKEN_BYTES = 1024 * 1024
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_DPAPI_ENTROPY = b"voice2text/oauth-refresh-token/v1"


class OAuthError(RuntimeError):
    """OAuth failure whose message never includes codes, tokens, or response bodies."""


class OAuthTokenRequestError(OAuthError):
    """Sanitized token-endpoint failure retaining only its HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"OAuth token request failed with status {status_code}")


class TokenProtectionError(RuntimeError):
    """Current-user token protection or storage failed without exposing token content."""


@dataclass(frozen=True, slots=True)
class PkcePair:
    """One RFC 7636 verifier and its SHA-256 challenge."""

    verifier: str = field(repr=False)
    challenge: str

    def __post_init__(self) -> None:
        if not _PKCE_PATTERN.fullmatch(self.verifier):
            raise ValueError("PKCE verifier must contain 43-128 unreserved characters")
        expected = pkce_challenge(self.verifier)
        if not hmac.compare_digest(self.challenge, expected):
            raise ValueError("PKCE challenge does not match its verifier")


@dataclass(frozen=True, slots=True)
class OAuthMetadata:
    """Strict subset of authorization-server metadata needed by this desktop app."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    scopes_supported: tuple[str, ...] = ()

    @classmethod
    def from_document(cls, document: object, *, server_url: str) -> OAuthMetadata:
        """Validate metadata for a same-origin public client using PKCE S256."""

        if not isinstance(document, dict):
            raise OAuthError("OAuth metadata was not a JSON object")
        normalized_server = normalize_server_url(server_url)
        issuer = _required_string(document, "issuer")
        if issuer.rstrip("/") != normalized_server:
            raise OAuthError("OAuth metadata issuer does not match the configured server")

        authorization_endpoint = _validate_same_origin_endpoint(
            _required_string(document, "authorization_endpoint"),
            server_url=normalized_server,
            label="authorization endpoint",
        )
        token_endpoint = _validate_same_origin_endpoint(
            _required_string(document, "token_endpoint"),
            server_url=normalized_server,
            label="token endpoint",
        )
        challenge_methods = _string_tuple(document, "code_challenge_methods_supported")
        if "S256" not in challenge_methods:
            raise OAuthError("OAuth server does not advertise the required PKCE S256 method")
        token_auth_methods = _string_tuple(
            document,
            "token_endpoint_auth_methods_supported",
        )
        if "none" not in token_auth_methods:
            raise OAuthError("OAuth server does not advertise public-client token exchange")

        scopes = _string_tuple(document, "scopes_supported", required=False)
        return cls(
            issuer=issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            scopes_supported=scopes,
        )


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """Browser URL plus in-memory values needed to validate and exchange its result."""

    url: str
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """Validated token response; secret values are excluded from representations."""

    access_token: str = field(repr=False)
    expires_at: float
    refresh_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_token_value(self.access_token, label="access token")
        if self.refresh_token is not None:
            _validate_token_value(self.refresh_token, label="refresh token")
        if self.expires_at <= 0:
            raise ValueError("token expiration must be positive")


class RefreshTokenStore(Protocol):
    """Minimal protected refresh-token storage surface used by `OAuthClient`."""

    def save(self, refresh_token: str) -> None: ...

    def load(self) -> str | None: ...

    def clear(self) -> None: ...


def create_pkce_pair(*, length: int = 64) -> PkcePair:
    """Generate a high-entropy verifier using only RFC 7636 unreserved characters."""

    if not 43 <= length <= 128:
        raise ValueError("PKCE verifier length must be between 43 and 128")
    verifier = "".join(secrets.choice(_PKCE_ALPHABET) for _ in range(length))
    return PkcePair(verifier=verifier, challenge=pkce_challenge(verifier))


def pkce_challenge(verifier: str) -> str:
    """Return the unpadded base64url SHA-256 challenge for a valid verifier."""

    if not _PKCE_PATTERN.fullmatch(verifier):
        raise ValueError("PKCE verifier must contain 43-128 unreserved characters")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def create_oauth_state() -> str:
    """Generate a one-time browser callback correlation value."""

    return secrets.token_urlsafe(32)


def validate_oauth_state(expected: str, received: str) -> None:
    """Compare callback state in constant time and reject missing values."""

    if not expected or not received or not hmac.compare_digest(expected, received):
        raise OAuthError("OAuth callback state validation failed")


def normalize_server_url(server_url: str) -> str:
    """Accept only a bare HTTPS tenant origin, never credentials or URL decorations."""

    try:
        parts = urlsplit(server_url)
        port = parts.port
    except ValueError as exc:
        raise OAuthError("Glean server URL is invalid") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise OAuthError("Glean server URL must be a bare HTTPS origin")
    host = parts.hostname.lower()
    netloc = host if port in {None, 443} else f"{host}:{port}"
    return urlunsplit(("https", netloc, "", "", ""))


def oauth_discovery_url(server_url: str) -> str:
    """Build the RFC 8414 metadata URL for the configured tenant origin."""

    return f"{normalize_server_url(server_url)}{_DISCOVERY_PATH}"


def discover_oauth_metadata(
    server_url: str,
    *,
    client: httpx.Client | None = None,
    timeout_seconds: float = 10.0,
) -> OAuthMetadata:
    """Fetch small same-tenant metadata without following redirects or exposing its body."""

    if not 1.0 <= timeout_seconds <= 30.0:
        raise ValueError("metadata timeout must be between 1 and 30 seconds")
    own_client = client is None
    http_client = client or httpx.Client(follow_redirects=False, timeout=timeout_seconds)
    try:
        response = http_client.get(
            oauth_discovery_url(server_url),
            headers={"Accept": "application/json"},
            follow_redirects=False,
            timeout=timeout_seconds,
        )
        if response.status_code != 200:
            raise OAuthError(f"OAuth metadata request failed with status {response.status_code}")
        if len(response.content) > _MAX_METADATA_BYTES:
            raise OAuthError("OAuth metadata response exceeded the size limit")
        try:
            document = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OAuthError("OAuth metadata response was not valid JSON") from exc
        return OAuthMetadata.from_document(document, server_url=server_url)
    except httpx.HTTPError as exc:
        raise OAuthError("OAuth metadata discovery failed") from exc
    finally:
        if own_client:
            http_client.close()


def build_authorization_request(
    metadata: OAuthMetadata,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    state: str | None = None,
    pkce: PkcePair | None = None,
) -> AuthorizationRequest:
    """Build a public-client Authorization Code request with PKCE S256."""

    _validate_client_id(client_id)
    validate_loopback_redirect_uri(redirect_uri)
    _validate_scopes(scopes)
    if metadata.scopes_supported:
        unsupported = set(scopes).difference(metadata.scopes_supported)
        if unsupported:
            raise OAuthError("Configured OAuth scopes are not advertised by the server")

    request_state = create_oauth_state() if state is None else state
    _validate_oauth_state_value(request_state)
    request_pkce = pkce or create_pkce_pair()
    endpoint = urlsplit(metadata.authorization_endpoint)
    existing = parse_qsl(endpoint.query, keep_blank_values=True, strict_parsing=True)
    protected_names = {
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
    }
    if any(name in protected_names for name, _value in existing):
        raise OAuthError("OAuth authorization endpoint contains conflicting parameters")
    query = urlencode(
        [
            *existing,
            ("response_type", "code"),
            ("client_id", client_id),
            ("redirect_uri", redirect_uri),
            ("scope", " ".join(scopes)),
            ("state", request_state),
            ("code_challenge", request_pkce.challenge),
            ("code_challenge_method", "S256"),
        ]
    )
    url = urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path, query, ""))
    return AuthorizationRequest(
        url=url,
        state=request_state,
        code_verifier=request_pkce.verifier,
    )


def validate_loopback_redirect_uri(redirect_uri: str) -> None:
    """Require an exact IPv4 loopback HTTP callback with an ephemeral TCP port."""

    try:
        parts = urlsplit(redirect_uri)
        port = parts.port
    except ValueError as exc:
        raise OAuthError("OAuth redirect URI is invalid") from exc
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.username is not None
        or parts.password is not None
        or port is None
        or parts.path != _CALLBACK_PATH
        or parts.query
        or parts.fragment
    ):
        raise OAuthError("OAuth redirect URI must be an exact IPv4 loopback callback")


def parse_authorization_callback(
    callback_url: str,
    *,
    expected_redirect_uri: str,
    expected_state: str,
) -> str:
    """Validate one browser callback and return only its short-lived authorization code."""

    if len(callback_url) > _MAX_CALLBACK_URL_LENGTH:
        raise OAuthError("OAuth callback exceeded the size limit")
    validate_loopback_redirect_uri(expected_redirect_uri)
    actual = urlsplit(callback_url)
    expected = urlsplit(expected_redirect_uri)
    if (
        actual.scheme != expected.scheme
        or actual.hostname != expected.hostname
        or actual.port != expected.port
        or actual.path != expected.path
        or actual.fragment
    ):
        raise OAuthError("OAuth callback did not match the loopback redirect URI")
    try:
        pairs = parse_qsl(
            actual.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=16,
        )
    except ValueError:
        raise OAuthError("OAuth callback query was invalid") from None
    values: dict[str, str] = {}
    for name, value in pairs:
        if name in values:
            raise OAuthError("OAuth callback contained a duplicate parameter")
        values[name] = value

    validate_oauth_state(expected_state, values.get("state", ""))
    if "error" in values:
        if values["error"] == "access_denied":
            raise OAuthError("OAuth authorization was denied or cancelled")
        raise OAuthError("OAuth authorization failed")
    code = values.get("code", "")
    if not code or len(code) > 8_192 or any(ord(character) < 0x20 for character in code):
        raise OAuthError("OAuth callback did not contain a valid authorization code")
    return code


class LoopbackCallbackServer:
    """Receive one OAuth redirect on 127.0.0.1 without logging request values."""

    def __init__(self, *, expected_state: str) -> None:
        if not expected_state:
            raise ValueError("expected OAuth state cannot be empty")
        self._expected_state = expected_state
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._code: str | None = None
        self._error: OAuthError | None = None
        self._redirect_uri: str | None = None

    @property
    def redirect_uri(self) -> str:
        if self._redirect_uri is None:
            raise RuntimeError("loopback callback server is not running")
        return self._redirect_uri

    def start(self) -> str:
        """Bind an ephemeral IPv4 loopback port and start the callback thread."""

        if self._server is not None:
            return self.redirect_uri
        self._done.clear()
        self._code = None
        self._error = None
        owner = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner._handle_get(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        except OSError as exc:
            raise OAuthError("Could not open the local OAuth callback listener") from exc
        port = int(server.server_address[1])
        self._server = server
        self._redirect_uri = f"http://127.0.0.1:{port}{_CALLBACK_PATH}"
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="voice2text-oauth-callback",
            daemon=True,
        )
        self._thread.start()
        return self._redirect_uri

    def wait_for_code(self, timeout_seconds: float = 180.0) -> str:
        """Wait for the validated callback without returning any other query values."""

        if timeout_seconds <= 0:
            raise ValueError("callback timeout must be positive")
        if self._server is None:
            raise RuntimeError("loopback callback server is not running")
        if not self._done.wait(timeout_seconds):
            raise OAuthError("OAuth browser callback timed out")
        if self._error is not None:
            raise self._error
        if self._code is None:  # pragma: no cover - invariant guard
            raise OAuthError("OAuth browser callback did not produce a code")
        return self._code

    def stop(self) -> None:
        """Close the callback port and remove in-memory code and state references."""

        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(2.0)
        self._code = None
        self._error = None
        self._redirect_uri = None
        self._expected_state = ""

    def __enter__(self) -> LoopbackCallbackServer:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.client_address[0] != "127.0.0.1":
            _send_browser_response(handler, 403, "Request rejected")
            return
        request_target = urlsplit(handler.path)
        if request_target.path != _CALLBACK_PATH:
            _send_browser_response(handler, 404, "Not found")
            return
        try:
            callback_url = f"{self.redirect_uri}{handler.path[len(_CALLBACK_PATH) :]}"
            self._code = parse_authorization_callback(
                callback_url,
                expected_redirect_uri=self.redirect_uri,
                expected_state=self._expected_state,
            )
        except OAuthError as exc:
            self._error = exc
            _send_browser_response(handler, 400, "Authorization could not be completed")
        else:
            _send_browser_response(handler, 200, "Authorization complete. You may close this tab.")
        finally:
            self._done.set()


class TokenProtector(Protocol):
    """Protect bytes for the current signed-in Windows user."""

    def protect(self, plaintext: bytes | bytearray) -> bytes: ...

    def unprotect(self, protected_data: bytes) -> bytearray: ...


class _DataBlob(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class CurrentUserDpapiProtector:
    """Wrap DPAPI without the machine-wide flag and with all Windows UI disabled."""

    def __init__(self, *, entropy: bytes = _DPAPI_ENTROPY) -> None:
        if sys.platform != "win32":
            raise OSError("DPAPI token protection is available only on Windows")
        if not entropy:
            raise ValueError("DPAPI optional entropy cannot be empty")
        self._entropy = bytes(entropy)
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def protect(self, plaintext: bytes | bytearray) -> bytes:
        if not plaintext:
            raise ValueError("token plaintext cannot be empty")
        input_blob, input_buffer = _blob_from_bytes(plaintext)
        entropy_blob, entropy_buffer = _blob_from_bytes(self._entropy)
        output_blob = _DataBlob()
        try:
            succeeded = self._crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "voice2text OAuth refresh token",
                ctypes.byref(entropy_blob),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
            if not succeeded:
                error = ctypes.get_last_error()
                raise TokenProtectionError(
                    f"Windows could not protect the refresh token (error {error})"
                )
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            ctypes.memset(input_buffer, 0, len(input_buffer))
            ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
            if output_blob.pbData:
                self._kernel32.LocalFree(output_blob.pbData)

    def unprotect(self, protected_data: bytes) -> bytearray:
        if not protected_data or len(protected_data) > _MAX_PROTECTED_TOKEN_BYTES:
            raise TokenProtectionError("protected refresh-token data is invalid")
        input_blob, input_buffer = _blob_from_bytes(protected_data)
        entropy_blob, entropy_buffer = _blob_from_bytes(self._entropy)
        output_blob = _DataBlob()
        description = ctypes.c_wchar_p()
        try:
            succeeded = self._crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                ctypes.byref(description),
                ctypes.byref(entropy_blob),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
            if not succeeded:
                error = ctypes.get_last_error()
                raise TokenProtectionError(
                    f"Windows could not unprotect the refresh token (error {error})"
                )
            return bytearray(ctypes.string_at(output_blob.pbData, output_blob.cbData))
        finally:
            ctypes.memset(input_buffer, 0, len(input_buffer))
            ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
            if output_blob.pbData:
                self._kernel32.LocalFree(output_blob.pbData)
            if description:
                self._kernel32.LocalFree(description)

    def _configure_signatures(self) -> None:
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = ctypes.c_int
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = ctypes.c_int
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p


class DpapiRefreshTokenStore:
    """Persist only a DPAPI ciphertext under the signed-in user's local profile."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        protector: TokenProtector | None = None,
    ) -> None:
        self._path = (path or _default_token_path()).expanduser().resolve()
        self._protector = protector or CurrentUserDpapiProtector()

    def save(self, refresh_token: str) -> None:
        """Replace the encrypted token atomically; plaintext is never written to disk."""

        if not refresh_token or "\0" in refresh_token:
            raise ValueError("refresh token is invalid")
        plaintext = bytearray(refresh_token.encode("utf-8"))
        try:
            protected = self._protector.protect(plaintext)
        finally:
            plaintext[:] = b"\0" * len(plaintext)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{secrets.token_hex(8)}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as token_file:
                token_file.write(protected)
                token_file.flush()
                os.fsync(token_file.fileno())
            os.replace(temporary, self._path)
        except OSError as exc:
            raise TokenProtectionError("Could not store the protected refresh token") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> str | None:
        """Return the current user's decrypted token, or None when no token is stored."""

        try:
            protected = self._path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TokenProtectionError("Could not read the protected refresh token") from exc
        plaintext = self._protector.unprotect(protected)
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError:
            raise TokenProtectionError("Protected refresh-token data was invalid") from None
        finally:
            plaintext[:] = b"\0" * len(plaintext)

    def clear(self) -> None:
        """Remove the protected token blob if it exists."""

        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            raise TokenProtectionError("Could not remove the protected refresh token") from exc


class OAuthClient:
    """Public desktop OAuth client with no client-secret support by design."""

    def __init__(
        self,
        *,
        server_url: str,
        client_id: str,
        scopes: tuple[str, ...] = ("CHAT",),
        token_store: RefreshTokenStore | None = None,
        http_client: httpx.Client | None = None,
        browser_opener: Callable[[str], bool] = webbrowser.open,
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._server_url = normalize_server_url(server_url)
        _validate_client_id(client_id)
        _validate_scopes(scopes)
        if not 1.0 <= timeout_seconds <= 120.0:
            raise ValueError("OAuth timeout must be between 1 and 120 seconds")
        self._client_id = client_id
        self._scopes = scopes
        self._token_store = token_store
        self._own_http_client = http_client is None
        self._http_client = http_client or httpx.Client(
            follow_redirects=False,
            timeout=timeout_seconds,
        )
        self._browser_opener = browser_opener
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._metadata: OAuthMetadata | None = None
        self._tokens: OAuthTokens | None = None
        self._lock = threading.Lock()

    def authorize(self, *, callback_timeout_seconds: float = 180.0) -> str:
        """Open the system browser, validate its loopback callback, and return an access token."""

        state = create_oauth_state()
        with self._lock, LoopbackCallbackServer(expected_state=state) as callback:
            metadata = self._get_metadata()
            request = build_authorization_request(
                metadata,
                client_id=self._client_id,
                redirect_uri=callback.redirect_uri,
                scopes=self._scopes,
                state=state,
            )
            try:
                opened = self._browser_opener(request.url)
            except Exception as exc:
                raise OAuthError("Could not open the system browser for sign-in") from exc
            if not opened:
                raise OAuthError("Could not open the system browser for sign-in")
            code = callback.wait_for_code(callback_timeout_seconds)
            tokens = self._request_tokens(
                metadata.token_endpoint,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": callback.redirect_uri,
                    "client_id": self._client_id,
                    "code_verifier": request.code_verifier,
                },
            )
            self._remember_tokens(tokens)
            return tokens.access_token

    def access_token(self) -> str:
        """Return a non-expiring-soon token, refreshing from DPAPI storage when needed."""

        with self._lock:
            if self._tokens is not None and self._tokens.expires_at - self._clock() > 60.0:
                return self._tokens.access_token
            refresh_token = self._token_store.load() if self._token_store is not None else None
            if not refresh_token:
                raise OAuthError("Glean sign-in is required")
            try:
                metadata = self._get_metadata()
                tokens = self._request_tokens(
                    metadata.token_endpoint,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self._client_id,
                        "scope": " ".join(self._scopes),
                    },
                )
            except OAuthTokenRequestError as exc:
                if exc.status_code in {400, 401}:
                    self._token_store.clear()
                raise
            self._remember_tokens(tokens, fallback_refresh_token=refresh_token)
            return tokens.access_token

    def invalidate_access_token(self) -> None:
        """Drop only the memory-resident access token after an HTTP 401."""

        with self._lock:
            self._tokens = None

    def sign_out(self) -> None:
        """Drop memory-resident tokens and remove any protected refresh token."""

        with self._lock:
            self._tokens = None
            if self._token_store is not None:
                self._token_store.clear()

    def close(self) -> None:
        """Drop access tokens and close only an internally owned HTTP client."""

        with self._lock:
            self._tokens = None
            if self._own_http_client:
                self._http_client.close()

    def __enter__(self) -> OAuthClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _get_metadata(self) -> OAuthMetadata:
        if self._metadata is None:
            self._metadata = discover_oauth_metadata(
                self._server_url,
                client=self._http_client,
                timeout_seconds=min(self._timeout_seconds, 30.0),
            )
            if self._metadata.scopes_supported:
                unsupported = set(self._scopes).difference(self._metadata.scopes_supported)
                if unsupported:
                    raise OAuthError("Configured OAuth scopes are not advertised by the server")
        return self._metadata

    def _request_tokens(self, token_endpoint: str, form: dict[str, str]) -> OAuthTokens:
        try:
            response = self._http_client.post(
                token_endpoint,
                data=form,
                headers={"Accept": "application/json"},
                follow_redirects=False,
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError:
            raise OAuthError("OAuth token request failed") from None
        if response.status_code != 200:
            raise OAuthTokenRequestError(response.status_code)
        if len(response.content) > _MAX_TOKEN_RESPONSE_BYTES:
            raise OAuthError("OAuth token response exceeded the size limit")
        try:
            document = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise OAuthError("OAuth token response was not valid JSON") from None
        return _parse_token_response(document, now=self._clock(), requested_scopes=self._scopes)

    def _remember_tokens(
        self,
        tokens: OAuthTokens,
        *,
        fallback_refresh_token: str | None = None,
    ) -> None:
        refresh_token = tokens.refresh_token or fallback_refresh_token
        self._tokens = OAuthTokens(
            access_token=tokens.access_token,
            expires_at=tokens.expires_at,
            refresh_token=refresh_token,
        )
        if refresh_token and "offline_access" in self._scopes and self._token_store is not None:
            self._token_store.save(refresh_token)


def _required_string(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise OAuthError(f"OAuth metadata is missing {name}")
    return value


def _string_tuple(
    document: dict[str, Any],
    name: str,
    *,
    required: bool = True,
) -> tuple[str, ...]:
    value = document.get(name)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise OAuthError(f"OAuth metadata has an invalid {name} value")
    return tuple(value)


def _validate_same_origin_endpoint(endpoint: str, *, server_url: str, label: str) -> str:
    parts = urlsplit(endpoint)
    server = urlsplit(server_url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or parts.hostname.lower() != server.hostname
        or parts.port != server.port
        or not parts.path.startswith("/")
    ):
        raise OAuthError(f"OAuth {label} must use the configured HTTPS origin")
    return endpoint


def _validate_client_id(client_id: str) -> None:
    if (
        not client_id
        or len(client_id) > 1_024
        or any(ord(character) < 0x20 for character in client_id)
    ):
        raise OAuthError("OAuth client ID is invalid")


def _validate_oauth_state_value(state: str) -> None:
    if not state or len(state) > 1_024 or any(ord(character) < 0x21 for character in state):
        raise OAuthError("OAuth state value is invalid")


def _validate_scopes(scopes: tuple[str, ...]) -> None:
    if not scopes or any(not _SCOPE_PATTERN.fullmatch(scope) for scope in scopes):
        raise OAuthError("OAuth scopes contain an invalid value")
    if len(scopes) != len(set(scopes)):
        raise OAuthError("OAuth scopes cannot contain duplicates")


def _validate_token_value(value: str, *, label: str) -> None:
    if not value or len(value) > 64 * 1024 or any(ord(character) < 0x20 for character in value):
        raise OAuthError(f"OAuth {label} is invalid")


def _parse_token_response(
    document: object,
    *,
    now: float,
    requested_scopes: tuple[str, ...],
) -> OAuthTokens:
    if not isinstance(document, dict):
        raise OAuthError("OAuth token response was not a JSON object")
    token_type = document.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise OAuthError("OAuth token response did not contain a Bearer token")
    access_token = document.get("access_token")
    if not isinstance(access_token, str):
        raise OAuthError("OAuth token response did not contain an access token")
    _validate_token_value(access_token, label="access token")
    expires_in = document.get("expires_in")
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, int | float)
        or not 1 <= expires_in <= 7 * 24 * 60 * 60
    ):
        raise OAuthError("OAuth token response contained an invalid expiration")
    refresh_token = document.get("refresh_token")
    if refresh_token is not None:
        if not isinstance(refresh_token, str):
            raise OAuthError("OAuth token response contained an invalid refresh token")
        _validate_token_value(refresh_token, label="refresh token")
    scope_value = document.get("scope")
    if scope_value is not None:
        if not isinstance(scope_value, str):
            raise OAuthError("OAuth token response contained invalid scopes")
        granted_scopes = tuple(scope_value.split())
        _validate_scopes(granted_scopes)
        if not set(requested_scopes).issubset(granted_scopes):
            raise OAuthError("OAuth token response omitted a required scope")
    return OAuthTokens(
        access_token=access_token,
        expires_at=now + float(expires_in),
        refresh_token=refresh_token,
    )


def _send_browser_response(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><title>voice2text</title></head>"
        f"<body><p>{message}</p></body></html>"
    ).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def _blob_from_bytes(value: bytes | bytearray) -> tuple[_DataBlob, Any]:
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _default_token_path() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        raise TokenProtectionError("LOCALAPPDATA is unavailable for protected token storage")
    return Path(local_app_data) / "voice2text" / "oauth_refresh_token.dpapi"
