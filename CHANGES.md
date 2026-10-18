# UBORA fork — changes from upstream

- **Upstream**: `RamsesAguirre777/facebook-ads-library-mcp`
- **Upstream SHA at fork time**: `ea3c877855c16fb4594f640634d06f85e414eadb` ("Make this a scraping-only Ad Library MCP")
- **Fork**: `joselo121-hash/facebook-ads-library-scraper-mcp`
- **License**: MIT, unchanged, original copyright notice preserved (`Copyright (c) 2025 Ramses Aguirre`).
- **Upstream not modified** — all changes live only in this fork.

All changes below were made for UBORA's Meta Spy V1 module (`docs/meta-spy/` in the
UBORA platform repo), after a real live audit found two confirmed, reproducible
blockers in the upstream server for UBORA's primary use case (commercial-ad research
in Colombia). See `docs/meta-spy/META_SPY_SCRAPER_LIVE_TEST.md` and
`META_SPY_PARSER_FIX_LIVE_TEST.md` in that repo for the full evidence trail.

## 1. Language-independent extraction (`facebook_ads_mcp_complete.py`)

**Why**: upstream's parser matched literal English UI text (`"Library ID:"`,
`**Sponsored**`, a fixed English CTA-label list) against the markdown-converted page.
Live-confirmed: for a Colombia request, Facebook serves the Ad Library in Spanish, and
upstream's parser extracted **zero ads** from pages that genuinely held dozens of real
commercial ads (confirmed via the page's own "~480 resultados" counter and real ad
copy visible in the raw markdown).

**What changed**: a new primary extraction path, `_extract_json_island_raw_ads` /
`_normalize_raw_ad` / `_extract_structured_ads`, parses the `<script
type="application/json" data-sjs>` Relay data-island tags Meta's own React frontend
embeds server-side for hydration, and walks them for any object carrying a real
`ad_archive_id`. These are JSON field names (`cta_type`, `page_name`, `link_url`,
`video_hd_url`, ...), not translated display text — confirmed live to work identically
for a Spanish-rendered page (English was not live-tested this session, since Facebook
never rendered English for a Colombia request; the schema itself is the same regardless
of display locale by construction, see the test fixture `test_json_island_is_language_
independent_english_fixture`).

The original markdown/regex parser is kept, renamed to
`_parse_ad_library_markdown_fallback`, and used **only** if the JSON-island extraction
returns zero ads (e.g. a future Facebook frontend change that stops embedding the data
island server-side).

**Fields gained that upstream never extracted**: `cta` as a stable enum (`SHOP_NOW`,
`LEARN_MORE`, ...) separate from the possibly-mixed-language `cta_label` display text;
direct `landing_url` (no `l.facebook.com` redirect decoding needed); `creative_video` /
`video_preview_image` (upstream had no video URL extraction at all); a real
`advertiser_page_id`; real `platforms` as a structured enum list (upstream's version
matched icon markers as plain text, which the icons rarely render as — "usually
empty", per upstream's own `docs/crawl4ai-implementation.md`).

## 2. Honest crawl4ai 403 handling (`facebook_ads_mcp_complete.py`)

**Why**: `crawl4ai>=0.9` (what `pip install crawl4ai` resolves to today, since
upstream's `requirements.txt` pins only a floor, `>=0.7.0`) added its own anti-bot
detector that marks **any** HTTP 403 response as blocked, regardless of content —
confirmed by reading `crawl4ai`'s own installed source
(`antibot_detector.py`/`async_webcrawler.py`). This defeats upstream's documented
design ("Facebook answers 403 but still serves the cards, so judge success by parsed
content, not status code") for anyone installing the project today.

**What changed**: `_render_ad_library` never reads `CrawlResult.success`. It inspects
the real `html`/`status_code` crawl4ai still returns regardless of its own internal
flag, and classifies honestly via `_classify_content`:
- real ads parsed → `ok`
- no ads, but a genuine zero-results page detected (Meta's own empty-state asset
  filename, `empty-state_overfiltering`, confirmed live and language-independent by
  construction; a locale-text fallback for the one phrase confirmed live in Spanish,
  `"No hay ningún anuncio que coincida..."`, plus two unverified English phrasings) →
  `zero_results`, a real, honest empty answer — never an error
- a 403/503 with a near-empty response (`<20000` bytes) → `likely_blocked`
- a real challenge/CAPTCHA page detected (checkpoint/security-check markers) →
  `likely_blocked`
- a full-size page with neither ads nor a recognised zero-results marker → conservative
  default, `likely_blocked` (never silently reported as a false empty result)

## 3. Hardening (§7 of the owner's request)

- `MAX_SCROLL_ROUNDS` / `MAX_WAIT_SECONDS` — hard ceilings regardless of caller input.
- `RENDER_RETRIES` + exponential backoff around the render step, for genuine transient
  failures (network errors, near-empty 403 responses) — never retried on a real
  zero-results page.
- `MAX_CONCURRENT_RENDERS` — a process-wide `asyncio.Semaphore` capping concurrent
  Chromium instances (each render is a full headless-browser session).
- Explicit dedupe by `ad_archive_id`, then by a stable per-card identity — the JSON
  walk can see the same ad more than once across nested Relay payloads, and the SPA's
  infinite scroll re-renders already-seen cards.
- Structured `logging` (timing, ad counts, classification outcome) with no sensitive
  data — this scraper never handles credentials or user data, only public ad content.

## 4. Tests (`tests/test_parser.py`)

Extended with fixtures built from the real field shapes confirmed live in this
session's testing (country=CO; real advertiser names/IDs/CDN URLs replaced with
clearly-fake placeholders of the same shape — never fabricated shapes): a JSON-island
image ad, a card with no CTA (1 of 80 real Colombia results had none), a video ad (3 of
80 real results), a DCO/multi-card ad (10 of 80 real results had multiple cards), the
dedupe path, the English-schema equivalent (language-independence test), and the full
`_classify_content` 3-way rule including the real zero-results marker. The original
English-markdown fixture and its tests are kept, renamed to match the new fallback
function name.

## 5. Streamable HTTP transport + service security (`security_http.py`, new)

**Why**: UBORA's owner decided `PROVIDER_HOSTING=SELF_HOSTED` /
`PROVIDER_TRANSPORT=STREAMABLE_HTTP` — STDIO stays for local dev only, never for the
UBORA integration (a shared STDIO subprocess has no per-request security boundary).
UBORA's Next.js backend already talks to one other MCP server over real streamable
HTTP (`src/server/external-integrations/mcp-client.ts`, `StreamableHTTPClientTransport`
from `@modelcontextprotocol/client` against `https://mcp.facebook.com/ads`) — this
fork now speaks the same real MCP-over-HTTP protocol, so UBORA's side can reuse that
exact client pattern instead of inventing a bespoke REST shape.

**What changed**:

- `mcp.run(transport="streamable-http", ...)` — FastMCP 4.1.1's native transport, not a
  hand-rolled HTTP layer (confirmed it supports this cleanly before writing anything —
  see live test evidence below). Selected via `MCP_TRANSPORT=streamable-http`; default
  stays `stdio`, so existing local-dev usage needs zero new env vars.
- `SharedSecretVerifier` (`security_http.py`) — a minimal `fastmcp.TokenVerifier`: one
  server-side-only bearer secret (`MCP_SHARED_SECRET`, required + rejected if under 16
  chars), constant-time compared (`hmac.compare_digest`), no OAuth machinery, no
  end-user identity — tenancy is UBORA's job, not this scraper's.
- `/healthz`, `/readyz` (`@mcp.custom_route`, confirmed unauthenticated by FastMCP's own
  design — `RequireAuthMiddleware` only wraps the `/mcp` route, not custom routes).
  Readiness is deliberately shallow (process-is-up only) — it never launches Chromium
  or hits facebook.com, so a health-check interval can't itself hammer the Ad Library.
- `CorrelationIdMiddleware`, `ConcurrencyLimitMiddleware`, `RequestTimeoutMiddleware` —
  written as **raw ASGI middleware**, not Starlette's `BaseHTTPMiddleware`. Live testing
  caught a real bug in an earlier `BaseHTTPMiddleware` version: for this server's SSE
  tool-call responses, `call_next()` returns as soon as response headers are ready, not
  when the streamed body (the actual scrape result) finishes — a 27s real tool call
  logged `elapsed_ms=624`, and a concurrency slot would have been released ~27s early.
  Rewritten as plain ASGI classes wrapping `send`/wrapping the whole `self.app(...)`
  call, which only returns after the full stream completes — re-tested live, now
  correct (see evidence below).
- `MaxBodySizeMiddleware` (`MCP_MAX_BODY_BYTES`, default 64KB) and `RateLimitMiddleware`
  (`MCP_RATE_LIMIT_PER_MINUTE`, default 60) — ordinary `BaseHTTPMiddleware` is correct
  here, since both act on the request only, before the response starts.
- `RequestTimeoutMiddleware` ceiling (`MCP_REQUEST_TIMEOUT_SECONDS`, default 120s): if
  the timeout fires before any response bytes went out, it returns a clean 504; if
  headers were already sent (confirmed the common case for a real multi-second scrape —
  SSE sends `200` + headers immediately, before the result event), it re-raises and the
  connection is cut instead of sending a malformed response. **UBORA's client-side
  provider must treat an aborted/reset stream as `PROVIDER_TIMEOUT`, not just a clean
  504** — this is the realistic failure shape, confirmed live, not the rarer clean-504
  one.
- `ConcurrencyLimitMiddleware` (`MCP_MAX_CONCURRENT_HTTP_REQUESTS`, default 4) guards the
  HTTP layer itself and is independent of the pre-existing `MAX_CONCURRENT_RENDERS`
  semaphore (§3), which only caps concurrent Chromium instances.
- `MCP_HTTP_HOST` / `MCP_HTTP_PORT` / `MCP_HTTP_PATH` / `MCP_ALLOWED_HOSTS` (passed
  straight through to FastMCP's own built-in `allowed_hosts` hardening — not
  reimplemented).

**Live evidence** (real HTTP calls against this process, real Chromium renders against
the real public Ad Library — not mocked):

| Check | Result |
|---|---|
| FastMCP 4.1.1 `streamable-http` transport | Confirmed via `inspect.signature` before coding — `run_http_async` natively supports `host/port/path/middleware/allowed_hosts/stateless_http` |
| `/healthz`, `/readyz` without auth | 200, 200 (real HTTP) |
| `/mcp` with no `Authorization` header | 401 (real HTTP) |
| `/mcp` with wrong bearer token | 401 (real HTTP) |
| `/mcp` with correct bearer token — real `initialize` + real `tools/call` | 200, real MCP protocol handshake, real `search_ad_library` result (68 structured ads, `country=CO`, query="Nike") |
| `elapsed_ms` logged vs curl's real wall-clock time | Before fix: `624ms` logged vs `27.4s` real (bug). After fix: `18589.4ms` logged vs `18.59s` real (curl `time_total`) — matches |
| Concurrency limit (`max_concurrent=1`), 2 real concurrent requests | 2nd request got real `429` at ~1.5s while the 1st (an actual 18.4s scrape) was still running — proves the slot stays held for the real duration, not time-to-first-byte |
| Rate limit (`max_requests_per_minute=3`), 5 rapid requests | 200, 200, 200, 429, 429 |
| Oversized body (`MCP_MAX_BODY_BYTES=200`, 500-byte body) | 413 |
| Request timeout (`MCP_REQUEST_TIMEOUT_SECONDS=5`) against a real ~15-20s scrape | Connection aborted after headers were already sent (curl exit 18, partial transfer) — the "re-raise, cut connection" branch, confirmed live. The "fires before headers sent → clean 504" branch is code-reviewed only, not independently live-reproduced this session (every real scrape call sent headers well before 5s) |

**Tests** (`tests/test_security_http.py`, 10 new, offline): `SharedSecretVerifier`
accept/reject/short-secret cases, `required_env`/`env_int` helpers, and
`RateLimitMiddleware`'s window logic via a dummy ASGI app. The concurrency/timeout
real-completion-timing behaviors are deliberately **not** re-proven with a mocked ASGI
call in this file — a mock would only test the mock, not real streaming timing; those
two are the live-evidence rows above instead.

## What was intentionally NOT changed

- The 2 tool signatures (`search_ad_library`, `scrape_ad_library_url`) — identical
  contract, so a caller (including UBORA's own future gateway) needs no changes beyond
  pointing at this fork.
- STDIO transport — unchanged, still the default, still zero-config for local dev.
- `advertiser_page_id`'s known unreliability under upstream's old parser turned out to
  be a non-issue with the new JSON-island extraction (confirmed live: 66 real ads from
  one real Page ID) — no special-casing was needed, so none was added.

## 6. Timeout/hang diagnostic + fix (2026-10-18, real incident: "waited 4m, then 10m")

**The real root cause, confirmed live with direct timed reproduction** (not theorized —
measured): this server's render retry loop could legitimately take up to **~282s** in the
worst case (3 attempts × ≤90s `page_timeout` each, plus 4s/8s backoff between them) — well
past any realistic HTTP request-timeout ceiling on the caller's side. When that ceiling
fires while a retry is genuinely still in progress, the HTTP connection gets cut after
response headers already went out. The MCP client then tries to reconnect (GET with
`Last-Event-ID`); this server is intentionally stateless (`stateless_http=True`, no event
store), so it has nothing to resume and correctly answers `405`. Critically, **the client
library does not reject the caller's pending `callTool()` promise when that reconnection
attempt fails** — confirmed by direct instrumented reproduction: a real call hung
completely silently from the moment the stream broke (5.3s) until the ONLY thing that
ever rescued it — the caller's own explicit `timeout` option — fired at exactly 600.1s
(10 minutes, the value used in the reproduction). Nothing else in the stack would have
rescued it sooner. This is a very close, direct match for the reported "waited 4m, then
10m on the retry" — not the work itself taking that long, but a hang with only the outer
timeout able to end it. Meanwhile, this server's own render task is **not** cancelled when
the HTTP layer gives up — confirmed live: a cut-off search still completed successfully
~20s later, invisible to the client that had already given up, burning Chromium/CPU for
nobody.

**Fix — `MAX_TOTAL_RENDER_SECONDS = 100`** (`facebook_ads_mcp_complete.py`): the retry loop
now tracks real elapsed time across all attempts and refuses to start (or back off into) a
retry that cannot also *finish* inside this budget — it returns the last real
classification (`ok`/`likely_blocked`/`network_error`) instead. This keeps a normal
response — even one that needed a retry — comfortably inside the HTTP layer's
`MCP_REQUEST_TIMEOUT_SECONDS` (120s default), so the mid-stream-cut scenario described
above is no longer reachable under realistic settings. **Deliberately does not change
`MCP_REQUEST_TIMEOUT_SECONDS` or any client-side timeout** — this closes the coordination
gap between the two, rather than trading one blind timeout increase for another.

**Known residual limitation, not solved by this fix** (flagged honestly, not papered
over): if a single attempt alone — or any other cause — still exceeds whatever HTTP
timeout is configured, the same hang-until-caller's-own-timeout behavior can still occur,
because real SSE resumability (an event store + session persistence) is not implemented
and is a materially bigger change than this incident justified on its own. A caller
talking to this provider must always set an explicit, bounded `callTool` timeout — UBORA's
own `MetaAdLibraryScraperProvider` already does (150s), independent of this fix.

**Stage timing + correlation id** (`_render_ad_library`, `search_ad_library`,
`security_http.py`'s `CorrelationIdMiddleware`): every response now carries a `stages`
object (`browser_start_ms`, `render_ms`, `parse_ms`, `retry_count`) and a `correlation_id`
that matches the SAME id the HTTP layer logs for that request — confirmed live, the
tool-level log line and the HTTP-layer completion log line now share one id end-to-end.
`get_http_headers()` (FastMCP) reads it; `CorrelationIdMiddleware` now injects a
newly-generated id back into the downstream request scope so a client that never sent one
still gets a single consistent id across every log line (a real gap found and fixed during
this same investigation — the first version only fixed the HTTP-layer log, not the tool's).
Honest limitation: crawl4ai's `arun()` is a single call covering page-load, hydration
*and* scroll together — `render_ms` cannot be split into separate `META_LOAD_MS`/
`SCROLL_MS` without deeper crawl4ai-internal hooks, which this fix does not add.

**Live evidence** (real server, real HTTP, real Chromium renders — not mocked):

| Check | Result |
|---|---|
| Reproduced the hang directly, with timing | Stream broke at 5.3s; call stayed pending with zero further activity; rejected only at 600.1s by the caller's own explicit timeout |
| Confirmed the server-side render is not cancelled by the HTTP cut | A cut-off search still completed successfully ~20s later, server-side only |
| Real search after the fix, default settings, no retry needed | 68 real ads, resolved cleanly at 26.98s, `stages={"retry_count":0,"browser_start_ms":900.5,"render_ms":25170.3,"parse_ms":24.1}` |
| Correlation id shared across HTTP-layer log, tool-level log, and the response sent to the caller | Confirmed identical id (`d1d3f9cb...`) in all three, same request |
| Deadline guard skips a retry it cannot finish in budget | Covered by `tests/test_render_budget.py` (deterministic, mocked clock — no real 90s wait) |
| Offline suite after all of the above | 37/37 pass |

**Not implemented, explicitly out of scope for this fix** (would need separate
authorization): real SSE resumability; disabling the MCP client's reconnection outright
(would not fix the actual hang — confirmed by reading the SDK's own reconnection-exhausted
code path, which never rejects the pending request either way — only makes the dead-end
arrive ~1ms faster); a warm Chromium/browser-context pool to cut the ~0.5-0.9s per-call
`browser_start_ms` cold-start cost (real, confirmed, but small relative to `render_ms` —
25.2s vs 0.9s in the measurement above — so not the dominant cost and not pursued here).
