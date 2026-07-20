"""Optional bearer-token auth for the API surface (ROADMAP Phase 0).

Threat model: the server binds 127.0.0.1 by default and needs no auth — but
``INSTITUTE_HOST`` is settable, and a LAN-exposed institute with zero auth is
the verified Phase 0 finding. The posture:

- ``INSTITUTE_TOKEN`` unset (the default) → **zero change**: the middleware
  passes every request through untouched, exactly today's single-operator
  localhost behaviour.
- ``INSTITUTE_TOKEN`` set → every ``/api/*`` request must carry
  ``Authorization: Bearer <token>`` or it gets a 401 before any routing.
  ``/health`` stays exempt (probes: launchd/monitoring/`institute status`),
  as do the SPA static routes (the HTML shell is not the secret — every
  data fetch it makes hits ``/api/*`` and is enforced).
- Binding a non-loopback host WITHOUT a token logs one startup warning
  (``install_auth``) — the misconfiguration the roadmap item exists for.

The token is re-read per request (cheap: one settings attribute / env probe),
so tests and runtime environment changes never fight a cached copy.
``settings.token`` is the normal source (including ``.env``); the raw process
``INSTITUTE_TOKEN`` variable remains a compatibility fallback.

Implementation is a pure ASGI middleware, not ``BaseHTTPMiddleware``: the
response is passed through verbatim with no wrapping, so the SSE event
stream (``/api/events/stream``) and NDJSON ask-stream keep their unbuffered
semantics.
"""
from __future__ import annotations

import logging
import os
import secrets

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import get_settings

log = logging.getLogger("institute.auth")

_EXEMPT_PATHS = ("/health",)  # exact-match exemptions (probes)


def configured_token() -> str | None:
    """The bearer token in force, or None (auth disabled).

    ``settings.token`` is authoritative when set; raw ``INSTITUTE_TOKEN`` is
    the compatibility fallback. Empty strings mean "unset".
    """
    token = getattr(get_settings(), "token", None) or os.environ.get("INSTITUTE_TOKEN")
    return token or None


class BearerAuthMiddleware:
    """401 any /api/* request without the right bearer token (when one is set)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = configured_token()
        if token is None:
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path in _EXEMPT_PATHS or not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        provided = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                provided = value.decode("latin-1")
                break
        # non-ASCII header bytes would make compare_digest raise TypeError
        # (500); fail closed with a clean 401 instead — such a value can never
        # equal the ASCII "Bearer <token>" anyway
        if provided.isascii() and secrets.compare_digest(provided, f"Bearer {token}"):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"detail": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


def install_auth(app: FastAPI) -> None:
    """Mount the middleware + warn on a non-loopback bind without a token.

    Called once from ``create_app()``. The warning fires at app construction,
    not per request: it flags the configuration, not the traffic.
    """
    settings = get_settings()
    if settings.host not in ("127.0.0.1", "localhost", "::1") and configured_token() is None:
        log.warning(
            "INSTITUTE_HOST=%s binds beyond loopback but INSTITUTE_TOKEN is not set — "
            "the API is reachable on the network with NO auth; set INSTITUTE_TOKEN",
            settings.host,
        )
    app.add_middleware(BearerAuthMiddleware)
