"""Unit tests for security_http.py — the parts that are pure/deterministic enough for
offline tests. Concurrency-release-on-real-completion and request-timeout-on-real-completion
are deliberately NOT re-proven here with a mocked ASGI call: that would only test the mock,
not real response-streaming timing. Those two behaviors were validated live against the real
HTTP server and a real Chromium render (see docs/meta-spy/META_SPY_HTTP_TRANSPORT_LIVE_TEST.md
in the UBORA repo) — this file covers what's safe and meaningful to assert without a live stack.
"""
import asyncio
import os

import pytest

from security_http import (
    SharedSecretVerifier, RateLimitMiddleware, env_int, required_env,
)


class TestSharedSecretVerifier:
    def test_rejects_short_secret(self):
        with pytest.raises(ValueError):
            SharedSecretVerifier("short")

    def test_rejects_empty_secret(self):
        with pytest.raises(ValueError):
            SharedSecretVerifier("")

    def test_accepts_matching_token(self):
        verifier = SharedSecretVerifier("a_real_shared_secret_value_here")
        token = asyncio.run(verifier.verify_token("a_real_shared_secret_value_here"))
        assert token is not None
        assert token.client_id == "ubora-platform"
        assert token.token == "***"  # never echoes the real secret back

    def test_rejects_wrong_token(self):
        verifier = SharedSecretVerifier("a_real_shared_secret_value_here")
        token = asyncio.run(verifier.verify_token("something_else_entirely"))
        assert token is None

    def test_rejects_empty_token(self):
        verifier = SharedSecretVerifier("a_real_shared_secret_value_here")
        token = asyncio.run(verifier.verify_token(""))
        assert token is None


class TestEnvHelpers:
    def test_required_env_raises_when_missing(self, monkeypatch):
        monkeypatch.delenv("SOME_TEST_VAR", raising=False)
        with pytest.raises(RuntimeError):
            required_env("SOME_TEST_VAR")

    def test_required_env_returns_value(self, monkeypatch):
        monkeypatch.setenv("SOME_TEST_VAR", "value123")
        assert required_env("SOME_TEST_VAR") == "value123"

    def test_env_int_default(self, monkeypatch):
        monkeypatch.delenv("SOME_INT_VAR", raising=False)
        assert env_int("SOME_INT_VAR", 42) == 42

    def test_env_int_parses_set_value(self, monkeypatch):
        monkeypatch.setenv("SOME_INT_VAR", "7")
        assert env_int("SOME_INT_VAR", 42) == 7


class _DummyASGIApp:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(path="/mcp"):
    return {"type": "http", "method": "POST", "path": path, "headers": []}


async def _run_once(app):
    sent = []
    async def send(message):
        sent.append(message)
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    await app(_http_scope(), receive, send)
    return sent


class TestRateLimitMiddleware:
    def test_allows_up_to_the_limit_then_rejects(self):
        inner = _DummyASGIApp()
        limited = RateLimitMiddleware(inner, max_requests_per_minute=2)

        async def go():
            results = []
            for _ in range(3):
                sent = await _run_once(limited)
                status = next(m["status"] for m in sent if m["type"] == "http.response.start")
                results.append(status)
            return results

        statuses = asyncio.run(go())
        assert statuses == [200, 200, 429]
        assert inner.calls == 2  # the 3rd request never reached the inner app
