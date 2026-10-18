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

## What was intentionally NOT changed

- The 2 tool signatures (`search_ad_library`, `scrape_ad_library_url`) — identical
  contract, so a caller (including UBORA's own future gateway) needs no changes beyond
  pointing at this fork.
- STDIO transport — unchanged; UBORA's own self-host integration plan (how a Next.js
  backend reaches a STDIO-only MCP server) is a separate, not-yet-decided question, out
  of scope for this fix.
- `advertiser_page_id`'s known unreliability under upstream's old parser turned out to
  be a non-issue with the new JSON-island extraction (confirmed live: 66 real ads from
  one real Page ID) — no special-casing was needed, so none was added.
