"""Service-to-service HTTP hardening for the Facebook Ad Library MCP (UBORA fork).

Everything here only matters when the server runs with ``MCP_TRANSPORT=streamable-http``.
STDIO (local dev, Claude Desktop, etc.) never touches this module — a local subprocess
has no network boundary to defend.

Tenancy, end-user identity and per-tenant rate limits are NOT this server's job — that
belongs to UBORA (the caller). This module only answers "is the caller UBORA's backend,
not a stranger on the network", plus baseline request hygiene (size, concurrency, pace,
timeout, correlation id, secret-free logs). One shared secret, one trusted caller.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from collections import deque
from contextvars import ContextVar

import anyio
from mcp.server.auth.provider import AccessToken
from fastmcp.server.auth.auth import TokenVerifier
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("facebook_ads_mcp.http")

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


class SharedSecretVerifier(TokenVerifier):
    """Verifies the single bearer token UBORA's backend sends. No OAuth, no per-user scopes."""

    def __init__(self, shared_secret: str):
        if not shared_secret or len(shared_secret) < 16:
            raise ValueError("MCP_SHARED_SECRET must be set and at least 16 characters long")
        super().__init__()
        self._shared_secret = shared_secret

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self._shared_secret):
            return None
        return AccessToken(token="***", client_id="ubora-platform", scopes=[])


class CorrelationIdMiddleware:
    """Assigns/propagates a request id and logs method/path/status/duration — never args, never the secret.

    Deliberately a raw ASGI middleware, not ``BaseHTTPMiddleware``: for this server's streamable-http
    tool calls, the response is a long-lived SSE stream, and ``BaseHTTPMiddleware``'s ``call_next()``
    returns as soon as the response headers are ready — NOT when the streamed body (i.e. the actual
    scrape result) finishes. Confirmed live: with ``call_next``, a 27s real tool call logged
    ``elapsed_ms=624`` (time-to-first-byte, not real duration). A plain ASGI app's
    ``await self.app(scope, receive, send)`` only returns once every chunk has been sent, so this
    measures the real end-to-end duration.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        correlation_id = headers.get(b"x-correlation-id", b"").decode() or uuid.uuid4().hex
        token = correlation_id_var.set(correlation_id)
        started = time.monotonic()
        status_code = 0

        # Inject the (possibly newly-generated) id back into the request headers seen
        # downstream — otherwise a client that never sent one gets a DIFFERENT id in this
        # middleware's own log line than whatever the tool later reads via
        # fastmcp's get_http_headers() (confirmed live: without this, the tool saw
        # correlation_id=None while this middleware logged a real generated id).
        downstream_scope = {**scope, "headers": [
            *[(k, v) for k, v in scope.get("headers", []) if k != b"x-correlation-id"],
            (b"x-correlation-id", correlation_id.encode()),
        ]}

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message = dict(message)
                message["headers"] = [*message.get("headers", []), (b"x-correlation-id", correlation_id.encode())]
            await send(message)

        try:
            await self.app(downstream_scope, receive, send_wrapper)
        except Exception:
            logger.exception("request failed path=%s correlation_id=%s", scope.get("path"), correlation_id)
            raise
        finally:
            correlation_id_var.reset(token)
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            logger.info("method=%s path=%s status=%s elapsed_ms=%s correlation_id=%s",
                        scope.get("method"), scope.get("path"), status_code, elapsed_ms, correlation_id)


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Rejects oversized request bodies before they reach tool argument parsing."""

    def __init__(self, app, max_bytes: int):
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_bytes:
                    return JSONResponse({"error": "request_body_too_large"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "invalid_content_length"}, status_code=400)
        return await call_next(request)


class ConcurrencyLimitMiddleware:
    """Caps in-flight HTTP requests server-wide — independent of the scraper's own render semaphore,
    which only guards concurrent Chromium instances. This guards the HTTP layer itself.

    Raw ASGI (not ``BaseHTTPMiddleware``) for the same reason as ``CorrelationIdMiddleware``: the
    slot must stay held until the streamed response body actually finishes, not until headers are
    ready — otherwise this would release capacity while the expensive Chromium render is still
    running, defeating the whole point of a concurrency limiter.
    """

    def __init__(self, app, max_concurrent: int):
        self.app = app
        self._semaphore = anyio.Semaphore(max_concurrent)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            self._semaphore.acquire_nowait()
        except anyio.WouldBlock:
            response = JSONResponse({"error": "too_many_concurrent_requests"}, status_code=429)
            return await response(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            self._semaphore.release()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Coarse, single-consumer fixed-window limiter. There is exactly one trusted caller
    (UBORA's backend) behind the shared secret, so a single process-wide window is honest —
    this is NOT a per-tenant rate limiter and must never be mistaken for one."""

    def __init__(self, app, max_requests_per_minute: int):
        super().__init__(app)
        self._max = max_requests_per_minute
        self._window: deque[float] = deque()

    async def dispatch(self, request: Request, call_next):
        now = time.monotonic()
        while self._window and now - self._window[0] > 60.0:
            self._window.popleft()
        if len(self._window) >= self._max:
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        self._window.append(now)
        return await call_next(request)


class RequestTimeoutMiddleware:
    """Aborts a request that runs far longer than the scraper's own internal render timeout
    ever should — a safety ceiling, not the primary timeout (that lives in the render logic).

    Raw ASGI: ``anyio.fail_after`` around ``await self.app(...)`` only bounds the real, full
    duration (including the streamed body) for a plain ASGI app — not just time-to-first-byte,
    which is all ``BaseHTTPMiddleware.call_next`` would have given it.
    """

    def __init__(self, app, timeout_seconds: float):
        self.app = app
        self._timeout_seconds = timeout_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started_response = False

        async def send_wrapper(message):
            nonlocal started_response
            if message["type"] == "http.response.start":
                started_response = True
            await send(message)

        try:
            with anyio.fail_after(self._timeout_seconds):
                await self.app(scope, receive, send_wrapper)
        except TimeoutError:
            if started_response:
                # Headers (or body chunks) already went out — too late for a clean 504, so
                # the connection is simply cut here rather than sending a malformed response.
                raise
            response = JSONResponse({"error": "request_timeout"}, status_code=504)
            await response(scope, receive, send)


async def health(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def readiness(_request: Request) -> Response:
    """Readiness is deliberately shallow: confirms the process answers HTTP at all.
    It does NOT launch Chromium or hit facebook.com — that would make a cheap liveness
    probe as slow/expensive as a real scrape, and would hammer the Ad Library on every
    orchestrator health-check interval."""
    return JSONResponse({"status": "ready"})


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required when MCP_TRANSPORT=streamable-http")
    return value


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default
