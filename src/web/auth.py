"""Simple session-based authentication for the DocMind web UI and REST API.

This module implements an opt-in API-key/password authentication scheme
appropriate for a self-hosted single-user deployment:

* When ``config.auth.enabled`` is False, every request passes through
  unchallenged — preserving the existing open behaviour.
* When enabled, every request must present either:
  - a valid signed session cookie (set by POST /login), or
  - an ``X-API-Key`` header matching ``config.auth.api_key``.

Session cookies are signed (HMAC-SHA256) and timestamped using only the
Python standard library — no extra dependency on itsdangerous. The
``api_key`` doubles as the login password.

Public routes (``/login``, ``/health``, ``/docs``, ``/openapi.json``,
``/redoc``, static files) are always accessible.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional, Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ..core.config import config

logger = logging.getLogger(__name__)

# Cookie name for the signed session token.
SESSION_COOKIE = "docmind_session"
SESSION_MAX_AGE_DEFAULT = 24 * 60 * 60  # 24h in seconds

# Routes that are always public — never challenged by auth.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/login",
        "/logout",
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)

# Path prefixes that are always public.
PUBLIC_PREFIXES: tuple[str, ...] = (
    "/docs",
    "/redoc",
    "/static/",
)


# ── Secrets & session token helpers ───────────────────────────────


def _secret() -> bytes:
    """Return the HMAC signing secret, deriving a stable default if unset.

    If ``config.auth.session_secret`` is empty, we derive a random
    per-process secret and cache it so that all calls within the same
    process return the same value (otherwise tokens signed at login
    would never verify). In production the operator should set
    ``DOCMIND_AUTH_SESSION_SECRET`` (or persist one in the DB via the
    settings page) so that sessions survive restarts. We log a warning
    when falling back.
    """
    raw = config.auth.session_secret
    if not raw:
        cached = getattr(_secret, "_fallback", None)
        if cached is None:
            if not getattr(_secret, "_warned", False):
                logger.warning(
                    "DOCMIND_AUTH_SESSION_SECRET is unset — using a random "
                    "per-process secret. Sessions will not survive restarts."
                )
                _secret._warned = True  # type: ignore[attr-defined]
            cached = secrets.token_hex(32)
            _secret._fallback = cached  # type: ignore[attr-defined]
        raw = cached
    return raw.encode("utf-8")


def _b64(b: bytes) -> str:
    """URL-safe base64 encoding without padding (for cookie-safe tokens)."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    """Inverse of _b64 — restores padding before decoding."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def create_session_token() -> str:
    """Create a signed, timestamped session token for the current config.

    The token encodes an issue-time stamp and an HMAC-SHA256 signature
    over that stamp using ``config.auth.session_secret``. It does NOT
    embed the api_key — possession of a valid signature proves the
    bearer authenticated at issue time.

    Returns:
        A compact, cookie-safe string ``<payload>.<sig>``.
    """
    payload = {"iat": int(time.time())}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_secret(), body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(sig)}"


def verify_session_token(token: str) -> bool:
    """Return True if ``token`` is a valid, non-expired session token.

    Uses constant-time comparison to resist timing attacks. Expiry is
    driven by ``config.auth.session_expiry_hours``.
    """
    if not token or "." not in token:
        return False
    body_b64, sig_b64 = token.rsplit(".", 1)
    try:
        body = _unb64(body_b64)
        sig = _unb64(sig_b64)
    except Exception:
        return False

    expected = hmac.new(_secret(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False

    try:
        payload = json.loads(body)
        iat = int(payload.get("iat", 0))
    except Exception:
        return False

    max_age = max(1, config.auth.session_expiry_hours) * 3600
    if time.time() - iat > max_age:
        return False
    return True


def set_session_cookie(response: Response) -> Response:
    """Attach a fresh session cookie to ``response``."""
    token = create_session_token()
    max_age = max(1, config.auth.session_expiry_hours) * 3600
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=False,  # self-hosted, often behind a reverse proxy on http
        path="/",
    )
    return response


def clear_session_cookie(response: Response) -> Response:
    """Delete the session cookie on ``response``."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ── Auth state & request inspection ───────────────────────────────


def auth_enabled() -> bool:
    """Return True if authentication is currently enabled.

    Reads the live ``config.auth.enabled`` flag, which may be toggled
    at runtime from the settings page.
    """
    return bool(config.auth.enabled)


def _is_public(path: str) -> bool:
    """Return True if ``path`` should never be challenged."""
    if path in PUBLIC_PATHS:
        return True
    for p in PUBLIC_PREFIXES:
        if path.startswith(p):
            return True
    # OpenAPI schema assets served under /docs or /redoc prefixes
    return False


def _has_valid_session(request: Request) -> bool:
    """Return True if the request carries a valid session cookie."""
    token = request.cookies.get(SESSION_COOKIE, "")
    return verify_session_token(token)


def _has_valid_api_key(request: Request) -> bool:
    """Return True if the request carries a valid X-API-Key header."""
    expected = config.auth.api_key
    if not expected:
        return False
    provided = request.headers.get("X-API-Key", "")
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def is_authenticated(request: Request) -> bool:
    """Return True if the request is authenticated (cookie OR api key)."""
    if _has_valid_session(request):
        return True
    if _has_valid_api_key(request):
        return True
    return False


# ── Login / logout helpers (used by server.py routes) ─────────────


def check_password(password: str) -> bool:
    """Return True if ``password`` matches the configured api_key."""
    expected = config.auth.api_key
    if not expected:
        return False
    if not password:
        return False
    return hmac.compare_digest(password, expected)


def login_response() -> Response:
    """Build a redirect-to-root response with a fresh session cookie."""
    resp = RedirectResponse(url="/", status_code=303)
    return set_session_cookie(resp)


def logout_response() -> Response:
    """Build a redirect-to-login response with a cleared session cookie."""
    resp = RedirectResponse(url="/login", status_code=303)
    return clear_session_cookie(resp)


def unauthorized_response(request: Request) -> Response:
    """Return the appropriate 401/redirect response for an unauth'd request.

    Browser (HTML) requests are redirected to /login; API/programmatic
    requests (X-API-Key header, JSON accept, or /api/ path) get a 401.
    """
    wants_html = _wants_html(request)
    if wants_html and not request.headers.get("X-API-Key"):
        return RedirectResponse(url="/login", status_code=303)
    return JSONResponse(
        status_code=401,
        content={"error": "unauthorized", "message": "Authentication required."},
    )


def _wants_html(request: Request) -> bool:
    """Heuristic: would the client prefer an HTML response?"""
    accept = request.headers.get("accept", "").lower()
    if "text/html" in accept:
        return True
    if request.url.path.startswith("/api/"):
        return False
    if request.headers.get("X-API-Key"):
        return False
    if "application/json" in accept:
        return False
    # No accept header or */* — treat as browser (HTML)
    return True


# ── Middleware ────────────────────────────────────────────────────


async def auth_middleware(request: Request, call_next) -> Response:
    """Starlette/FastAPI middleware: enforce auth on non-public routes.

    When ``config.auth.enabled`` is False this is a no-op pass-through.
    """
    if not auth_enabled():
        return await call_next(request)

    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    if is_authenticated(request):
        return await call_next(request)

    return unauthorized_response(request)


# ── Boot-time hydration from DB settings ──────────────────────────


def apply_auth_settings_from_db(settings: dict[str, str]) -> None:
    """Hydrate the in-memory AuthConfig from DB-stored settings.

    Called at startup (after DB connect) and whenever the settings page
    saves auth configuration. Keys recognized:

    - ``auth_enabled``  -> "1"/"0"
    - ``auth_api_key``  -> the configured password / api key
    - ``auth_session_secret`` -> stable signing secret (generated once)
    """
    enabled = settings.get("auth_enabled", "")
    if enabled:
        config.auth.enabled = enabled == "1"

    stored_key = settings.get("auth_api_key", "")
    if stored_key:
        config.auth.api_key = stored_key

    stored_secret = settings.get("auth_session_secret", "")
    if stored_secret:
        config.auth.session_secret = stored_secret


def ensure_session_secret() -> str:
    """Return a stable random session secret, generating one if needed.

    Used by the settings page handler when enabling auth for the first
    time — the generated value should be persisted to the DB so that
    sessions survive restarts.
    """
    if not config.auth.session_secret:
        config.auth.session_secret = secrets.token_hex(32)
    return config.auth.session_secret


def generate_api_key() -> str:
    """Generate a fresh random api key (for first-time enable)."""
    return "dm_" + secrets.token_urlsafe(24)
