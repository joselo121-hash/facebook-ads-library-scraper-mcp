"""Tests for the render-loop deadline guard (Fase 3 timeout diagnostic, 2026-10-18).

Live-confirmed incident: Python's own retry loop could legitimately run up to ~282s
(3 attempts x <=90s page_timeout + 4s/8s backoff) — well past the HTTP transport's
MCP_REQUEST_TIMEOUT_SECONDS (120s default), which cuts the connection mid-attempt,
triggering a client-side hang (see CHANGES.md and the UBORA repo's
docs/meta-spy/META_SPY_TIMEOUT_DIAGNOSTIC.md for the full chain). MAX_TOTAL_RENDER_SECONDS
fixes the coordination gap: never start a retry that cannot also finish inside budget.

These tests fake only the render step (`AsyncWebCrawler`) and the clock (`time.monotonic`)
so the retry-skipping *decision* is exercised deterministically and fast — no real network
call, no real 90s wait. The real end-to-end timing (a genuine search against the live Ad
Library) is validated separately, live, not here — see the diagnostic doc.
"""
import asyncio
import types

import facebook_ads_mcp_complete as m


class _FakeMarkdown:
    raw_markdown = ""


class _FakeCrawlResult:
    def __init__(self, status_code: int, html: str = ""):
        self.status_code = status_code
        self.html = html
        self.markdown = _FakeMarkdown()


class _FakeCrawler:
    """Stands in for crawl4ai's AsyncWebCrawler — returns a near-empty 403 every time,
    i.e. a result `_render_ad_library` always classifies as `likely_blocked`, so every
    attempt wants to retry (the scenario the deadline guard has to intervene in)."""
    call_count = 0

    def __init__(self, config=None):
        pass

    async def __aenter__(self):
        _FakeCrawler.call_count += 1
        return self

    async def __aexit__(self, *exc):
        return False

    async def arun(self, url: str, config=None):
        return _FakeCrawlResult(status_code=403, html="x" * 100)  # near-empty -> likely_blocked


def test_deadline_guard_skips_a_retry_it_cannot_finish_in_budget(monkeypatch):
    """Simulates real wall-clock time advancing well into the budget after the first
    attempt (e.g. a slow render) — the loop must stop instead of starting attempt 2."""
    _FakeCrawler.call_count = 0
    monkeypatch.setattr(m, "AsyncWebCrawler", _FakeCrawler)
    monkeypatch.setattr(m, "RENDER_RETRIES", 2)
    monkeypatch.setattr(m, "MAX_TOTAL_RENDER_SECONDS", 100)
    monkeypatch.setattr(m.asyncio, "sleep", lambda *_a, **_k: asyncio.sleep(0))  # skip real backoff waits

    # Fake clock: starts at 0, jumps forward by 85s on every read after the first attempt —
    # emulating a first attempt that itself took most of the budget (a real, slow render).
    clock = {"t": 0.0, "reads": 0}

    def fake_monotonic():
        clock["reads"] += 1
        if clock["reads"] > 2:  # let the loop's own bookkeeping read the clock a couple of times for "free"
            clock["t"] += 42.0
        return clock["t"]

    monkeypatch.setattr(m.time, "monotonic", fake_monotonic)

    result = asyncio.run(m._render_ad_library("https://www.facebook.com/ads/library/?q=x", wait_seconds=3, scroll_rounds=0))

    assert result["outcome"] == m.RenderOutcome.LIKELY_BLOCKED
    # Only the first attempt ran — the fake clock makes it look like that attempt alone
    # already consumed most of the budget (a real slow render), so a 2nd attempt is
    # correctly skipped: starting one could not have finished inside MAX_TOTAL_RENDER_SECONDS.
    assert _FakeCrawler.call_count == 1


def test_deadline_guard_never_blocks_the_first_attempt(monkeypatch):
    """The first attempt always runs regardless of budget — there is no earlier point
    to bail out from, and a request needs at least one real try."""
    _FakeCrawler.call_count = 0
    monkeypatch.setattr(m, "AsyncWebCrawler", _FakeCrawler)
    monkeypatch.setattr(m, "MAX_TOTAL_RENDER_SECONDS", 1)  # absurdly tight — still must try once
    result = asyncio.run(m._render_ad_library("https://www.facebook.com/ads/library/?q=x", wait_seconds=3, scroll_rounds=0))
    assert _FakeCrawler.call_count == 1
    assert result["outcome"] == m.RenderOutcome.LIKELY_BLOCKED


def test_stages_dict_reports_retry_count_and_timings(monkeypatch):
    class _OkCrawler(_FakeCrawler):
        async def arun(self, url: str, config=None):
            return _FakeCrawlResult(status_code=200, html="x" * 50_000)

    monkeypatch.setattr(m, "AsyncWebCrawler", _OkCrawler)
    result = asyncio.run(m._render_ad_library("https://www.facebook.com/ads/library/?q=x", wait_seconds=3, scroll_rounds=0))
    assert result["outcome"] == m.RenderOutcome.OK
    stages = result["stages"]
    assert stages["retry_count"] == 0
    assert isinstance(stages["browser_start_ms"], float)
    assert isinstance(stages["render_ms"], float)


def test_correlation_id_returns_none_outside_an_http_request():
    # No FastMCP HTTP request context is active in a plain pytest process (this mirrors
    # STDIO transport, which also has none) — must degrade to None, never raise.
    assert m._correlation_id() is None
