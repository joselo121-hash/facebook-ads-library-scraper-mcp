"""Facebook Ad Library MCP — a free, self-hosted scraper for the public Ad Library.

Meta's official ads_archive API only returns political & issue ads worldwide (all ad
types only for the EU/UK), so it is useless for researching ordinary commercial
advertisers. This server skips the API entirely: it renders the public Ad Library web
app in headless Chromium (via crawl4ai), scrolls it, and parses the ad cards into
structured records. No token, no account, any country.

UBORA fork (joselo121-hash/facebook-ads-library-scraper-mcp) — changes from upstream
(RamsesAguirre777/facebook-ads-library-mcp @ ea3c8778):

1. Language-independent extraction. The public Ad Library renders in the viewer's
   locale (Spanish for a Colombia request, confirmed live) — upstream's parser matched
   literal English UI text ("Library ID:", "**Sponsored**", a fixed English CTA-label
   list) against the markdown-converted page, so it silently extracted ZERO ads for any
   non-English locale even when the page held real results. This fork extracts from the
   JSON "data island" <script type="application/json" data-sjs> tags Meta's own
   React/Relay frontend embeds server-side for hydration — the exact same data the UI
   renders FROM, keyed by stable field names (`ad_archive_id`, `cta_type`, `page_name`,
   `link_url`, ...) that never change with display language. The old markdown-regex
   parser is kept as `_parse_ad_library_markdown_fallback` and used only if the JSON
   island extraction finds nothing — never the primary path.
2. Correct crawl4ai 0.9.5+ 403 handling. crawl4ai added its own anti-bot detector that
   marks ANY HTTP 403 response as blocked regardless of content, which defeated this
   project's documented design ("Facebook answers 403 but still serves the cards, so
   judge success by parsed content"). This fork never reads `CrawlResult.success` —
   it always inspects the real `html`/`status_code` crawl4ai still returns, classifies
   honestly (ads parsed / genuine zero results / likely blocked), and only then decides.
3. Hardening: capped `scroll_rounds`, a real retry+backoff loop around the render step,
   a process-wide concurrency limit (each call spins up a full Chromium instance), and
   explicit dedupe by `ad_archive_id` (the JSON island walk can see the same ad more
   than once across nested Relay payloads).

Tools (unchanged contract):
  - search_ad_library(query, country, ...)   keyword search
  - scrape_ad_library_url(url, ...)           scrape a specific Ad Library URL
"""
import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse, parse_qs, unquote

from fastmcp import FastMCP
from crawl4ai import AsyncWebCrawler

logger = logging.getLogger("facebook_ads_mcp")

mcp = FastMCP(
    name="Facebook Ad Library",
    instructions="""
    Scrapes the public Facebook Ad Library (no API, no token) for competitive ad research.
    Works for commercial ads in any country, any display language. Returns structured ad
    cards: advertiser, run date, creative count, landing domain, CTA, headline and body
    copy, plus image/video URLs when the ad has them.
    """,
)

AD_LIBRARY_BASE = "https://www.facebook.com/ads/library/"

# --- Hardening limits (§7) -------------------------------------------------------
MAX_SCROLL_ROUNDS = 20          # hard ceiling regardless of what the caller passes
MAX_WAIT_SECONDS = 30           # hard ceiling on initial hydration wait
RENDER_RETRIES = 2              # additional attempts after the first, on transient failure
RETRY_BACKOFF_BASE_SECONDS = 4  # exponential backoff: 4s, 8s, ...
MAX_CONCURRENT_RENDERS = 2      # each render = one headless Chromium instance

_render_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RENDERS)


# ---------------------------------------------------------------------------------
# Rendering — crawl4ai wrapper that never trusts CrawlResult.success (§2)
# ---------------------------------------------------------------------------------

class RenderOutcome:
    """Honest, 3-way classification of a render attempt — never a 2-way success/fail."""
    OK = "ok"                      # usable HTML returned, whether or not it has ads
    LIKELY_BLOCKED = "blocked"     # HTTP 403/503 and the HTML has no usable content
    NETWORK_ERROR = "network_error"


async def _render_ad_library(url: str, wait_seconds: int = 8, scroll_rounds: int = 8) -> Dict[str, Any]:
    """Render the Ad Library SPA and return its raw HTML + markdown + an honest status.

    Facebook frequently answers the headless browser with HTTP 403 while still serving
    the fully rendered page — crawl4ai>=0.9 now flags that as `success=False` on its own
    (its built-in anti-bot heuristic), which this function deliberately ignores. The only
    signal that matters is whether real content came back, judged after the fact by the
    caller (did the JSON-island/markdown parse find ads, or a genuine zero-results page).

    The Ad Library is an infinite-scroll SPA — the first paint only holds ~20-26 cards.
    ``scroll_rounds`` drives that many extra scroll-to-bottom + wait cycles so lazy-loaded
    cards are in the DOM before it is serialised. 0 = first render only (fast).
    """
    from crawl4ai import CrawlerRunConfig, BrowserConfig, CacheMode

    wait_seconds = min(max(3, wait_seconds), MAX_WAIT_SECONDS)
    rounds = min(max(0, int(scroll_rounds)), MAX_SCROLL_ROUNDS)

    bc = BrowserConfig(headless=True, browser_type="chromium",
                       viewport_width=1400, viewport_height=1600)
    hydrate_ms = wait_seconds * 1000
    js = (
        f"await new Promise(r=>setTimeout(r,{hydrate_ms}));"
        + "".join(
            "window.scrollTo(0, document.body.scrollHeight);"
            "await new Promise(r=>setTimeout(r,2500));"
            for _ in range(rounds)
        )
        + "window.scrollTo(0, document.body.scrollHeight);"
    )
    cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        delay_before_return_html=wait_seconds,
        page_timeout=max(90000, 20000 + rounds * 3000),
        js_code=[js],
        simulate_user=True,
        magic=True,
    )

    last_error: Optional[str] = None
    for attempt in range(RENDER_RETRIES + 1):
        async with _render_semaphore:
            try:
                async with AsyncWebCrawler(config=bc) as crawler:
                    r = await crawler.arun(url=url, config=cfg)
            except Exception as e:  # genuine network/browser failure, not a 403
                last_error = str(e)
                logger.warning("render attempt %d/%d raised: %s", attempt + 1, RENDER_RETRIES + 1, last_error)
                if attempt < RENDER_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt))
                    continue
                return {"outcome": RenderOutcome.NETWORK_ERROR, "html": "", "markdown": "",
                        "status_code": None, "error": last_error}

        html = r.html or ""
        md = r.markdown.raw_markdown if hasattr(r.markdown, "raw_markdown") else str(r.markdown or "")
        status_code = r.status_code

        # Honest classification (§2): a 403/503 with a near-empty body is a real block;
        # a 403/503 with a substantial HTML body is Facebook's known "serves it anyway"
        # behaviour — hand it to the caller, which decides from parsed content.
        if status_code in (403, 503) and len(html) < 20000:
            last_error = f"HTTP {status_code} with near-empty response ({len(html)} bytes)"
            logger.warning("render attempt %d/%d looks genuinely blocked: %s", attempt + 1, RENDER_RETRIES + 1, last_error)
            if attempt < RENDER_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            return {"outcome": RenderOutcome.LIKELY_BLOCKED, "html": html, "markdown": md,
                     "status_code": status_code, "error": last_error}

        return {"outcome": RenderOutcome.OK, "html": html, "markdown": md,
                 "status_code": status_code, "error": None}

    return {"outcome": RenderOutcome.NETWORK_ERROR, "html": "", "markdown": "",
             "status_code": None, "error": last_error or "exhausted retries"}


# ---------------------------------------------------------------------------------
# Primary extraction — Relay/SJS JSON data islands (§1, language-independent)
# ---------------------------------------------------------------------------------

_SJS_SCRIPT_RE = re.compile(
    r'<script type="application/json"[^>]*data-sjs[^>]*>(.*?)</script>', re.DOTALL)
_HANDLE_FROM_URI_RE = re.compile(r'facebook\.com/([^/?]+)/?')


def _handle_from_profile_uri(uri: Optional[str]) -> Optional[str]:
    if not uri:
        return None
    m = _HANDLE_FROM_URI_RE.search(uri)
    return m.group(1) if m else None


def _epoch_to_iso_date(epoch: Any) -> Optional[str]:
    if epoch is None:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _walk_for_ad_archive(obj: Any, out: Dict[str, dict]) -> None:
    """Recursively collect every dict carrying a real `ad_archive_id` — the stable,
    language-independent identifier Meta's own frontend uses for each ad."""
    if isinstance(obj, dict):
        aid = obj.get("ad_archive_id")
        if isinstance(aid, str) and aid.isdigit():
            out.setdefault(aid, obj)
        for v in obj.values():
            _walk_for_ad_archive(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_ad_archive(item, out)


def _extract_json_island_raw_ads(html: str) -> Dict[str, dict]:
    """Parse every Relay/SJS data-island script tag as JSON and collect raw ad objects,
    keyed and deduplicated by `ad_archive_id` (§7 dedupe) — the same payload can appear
    in more than one nested Relay preloader block."""
    ads_by_id: Dict[str, dict] = {}
    for block in _SJS_SCRIPT_RE.findall(html):
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        _walk_for_ad_archive(data, ads_by_id)
    return ads_by_id


def _normalize_raw_ad(raw: dict) -> List[dict]:
    """Map one raw `ad_archive_id` object to 1+ UBORA-shaped ad records — DCO/carousel
    ads carry multiple `snapshot.cards[]`, each becoming its own record (same convention
    upstream used for its markdown-based DCO handling), a simple ad uses `snapshot`
    itself as its single implicit card. Every field is `None` when genuinely absent —
    never inferred or fabricated."""
    aid = raw.get("ad_archive_id")
    snapshot = raw.get("snapshot") or {}
    cards = snapshot.get("cards") or []
    base_cards = cards if cards else [snapshot]
    platforms = raw.get("publisher_platform") or snapshot.get("publisher_platform") or []

    out: List[dict] = []
    for card in base_cards:
        image_url = card.get("original_image_url") or card.get("resized_image_url")
        video_url = card.get("video_hd_url") or card.get("video_sd_url")
        landing_url = card.get("link_url")
        media_type = "VIDEO" if video_url else ("IMAGE" if image_url else None)
        out.append({
            "library_id": aid,
            "ad_details_url": f"https://www.facebook.com/ads/library/?id={aid}",
            "advertiser": raw.get("page_name") or snapshot.get("page_name"),
            "advertiser_handle": _handle_from_profile_uri(snapshot.get("page_profile_uri")),
            "advertiser_page_id": raw.get("page_id") or snapshot.get("page_id"),
            "started_running": _epoch_to_iso_date(raw.get("start_date")),
            "ended_running": _epoch_to_iso_date(raw.get("end_date")),
            "is_active": raw.get("is_active"),
            "ads_using_creative": raw.get("collation_count") or 1,
            "body": card.get("body"),
            "headline": card.get("title"),
            "link_text": card.get("title"),
            # `cta` is the stable enum (SHOP_NOW, LEARN_MORE, ...) — never translated.
            # `cta_label` is Facebook's own display text for the button and MAY be in a
            # different language than the rest of the page (observed live) — exposed
            # as a secondary, display-only field, never used for matching/logic.
            "cta": card.get("cta_type"),
            "cta_label": card.get("cta_text"),
            "landing_url": landing_url,
            "landing_domain": urlparse(landing_url).netloc if landing_url else None,
            "creative_image": image_url,
            "creative_video": video_url,
            "video_preview_image": card.get("video_preview_image_url"),
            "media_type": media_type,
            "platforms": list(platforms) if platforms else [],
            "page_avatar": snapshot.get("page_profile_picture_url"),
            "extraction_method": "json_island",
        })
    return out


def _extract_structured_ads(html: str) -> List[dict]:
    raw_ads = _extract_json_island_raw_ads(html)
    results: List[dict] = []
    seen_cards: set = set()
    for raw in raw_ads.values():
        for ad in _normalize_raw_ad(raw):
            # a single DCO ad can repeat the exact same card content across multiple
            # scroll passes — dedupe on (library_id, landing_url) as a stable card key.
            # str()-coerced defensively: a malformed Relay payload could in theory put a
            # non-scalar in either field, and a dedupe key must never crash extraction.
            identity = ad.get("landing_url") or ad.get("body") or ad.get("headline") or ""
            key = (str(ad["library_id"]), str(identity))
            if key in seen_cards:
                continue
            seen_cards.add(key)
            results.append(ad)
    return results


# ---------------------------------------------------------------------------------
# Fallback extraction — the original markdown/regex parser (§1, locale-dependent)
# ---------------------------------------------------------------------------------
# Used ONLY when the JSON-island extraction above finds nothing — e.g. a future
# Facebook frontend change that stops embedding Relay data islands server-side. Kept
# verbatim from upstream (ea3c8778) except for the rename, so an English-locale render
# (or a future caller that forces `l=en`) still parses even if the JSON path ever
# breaks. NEVER the primary path — see the module docstring, §1.

def _decode_landing_url(fb_link: str) -> str:
    """Turn an l.facebook.com/l.php?u=... redirect into the real destination URL."""
    try:
        qs = parse_qs(urlparse(fb_link).query)
        if "u" in qs:
            return unquote(qs["u"][0])
    except Exception:
        pass
    return fb_link


_CTA_LABELS = (
    "Learn more", "Sign up", "Shop now", "Book now", "Send message",
    "Send WhatsApp message", "Contact us", "Subscribe", "Get offer", "Get quote",
    "Apply now", "Download", "Watch more", "See menu", "Order now", "Donate now",
    "Play game", "Listen now", "Get showtimes", "Save", "Open link", "Message page",
    "Call now", "Get directions", "Follow page", "Use app", "Install now",
    "Buy tickets", "Request time", "Try in camera",
)
_CTA_RE = re.compile(
    r'\s(' + '|'.join(re.escape(c) for c in _CTA_LABELS) +
    r')\s+\]\(https://l\.facebook\.com/l\.php', re.IGNORECASE)


def _parse_ad_library_markdown_fallback(md: str) -> List[dict]:
    """English-locale-only, best-effort structured extraction from rendered markdown.
    Fallback path — see module docstring §1. Never used unless the JSON-island
    extraction (`_extract_structured_ads`) returns zero ads."""
    ads = []
    seen = set()
    chunks = re.split(r'\nLibrary ID:\s*', md)
    for chunk in chunks[1:]:
        lib_id = re.match(r'(\d+)', chunk)
        if not lib_id:
            continue
        lid = lib_id.group(1)
        if lid in seen:
            continue
        seen.add(lid)
        ad = {
            "library_id": lid,
            "ad_details_url": f"https://www.facebook.com/ads/library/?id={lid}",
            "extraction_method": "markdown_fallback",
        }

        m = re.search(r'Started running on ([A-Za-z]{3} \d{1,2}, \d{4})', chunk)
        ad["started_running"] = m.group(1) if m else None

        m = re.search(r'\*\*(\d+)\s+ads?\*\*\s+use this creative', chunk)
        ad["ads_using_creative"] = int(m.group(1)) if m else 1

        m = re.search(r'\[([^\]]+)\]\(https://www\.facebook\.com/([^/)]+)/?\)', chunk)
        if m:
            ad["advertiser"] = m.group(1)
            ad["advertiser_handle"] = m.group(2)

        m = re.search(r'\(https://l\.facebook\.com/l\.php\?u=([^)&]+)', chunk)
        if m:
            real = _decode_landing_url("https://l.facebook.com/l.php?u=" + m.group(1))
            ad["landing_url"] = real
            ad["landing_domain"] = urlparse(real).netloc

        m = _CTA_RE.search(chunk)
        ad["cta"] = m.group(1).strip() if m else None

        m = re.search(r'\((https://scontent[^)]+?\.(?:jpe?g|png|webp)[^)]*)\)', chunk)
        ad["creative_image"] = m.group(1) if m else None
        if ad["cta"]:
            m = re.search(
                r'\[!\[[^\]]*\]\(https?://scontent[^)]+\)\s+(.+?)\s+' + re.escape(ad["cta"]) +
                r'\s+\]\(https://l\.facebook\.com', chunk, re.DOTALL)
            if m:
                link_text = re.sub(r'\s+', ' ', m.group(1)).strip()
                link_text = re.sub(r'^[A-Z0-9][A-Z0-9.\-]{2,}\s+', '', link_text)
                ad["link_text"] = link_text[:400]

        body = re.search(r'\*\*Sponsored\*\*\s*\n(.+?)(?:\n\[!\[|\nActive\n|\nInactive\n|$)',
                         chunk, re.DOTALL)
        if body:
            txt = re.sub(r'\s+\n', '\n', body.group(1)).strip()
            txt = re.sub(r'\[([^\]]+)\]\(https?://l\.facebook\.com[^)]*\)', r'\1', txt)
            txt = re.sub(r'\s*\[[^\]]*\]\([^)]*$', '', txt).strip()
            ad["body"] = txt[:2000]

        ad["platforms"] = [p for p in ("Facebook", "Instagram", "Audience Network", "Messenger")
                           if p in chunk]
        ads.append(ad)
    return ads


# ---------------------------------------------------------------------------------
# Result classification — §2's 3-way rule, applied once content is in hand
# ---------------------------------------------------------------------------------

# Primary zero-results signal: the filename of Meta's own empty-state illustration —
# confirmed live (real query "xzqwkjploiuyqqqnonsenseadvertiser999", country=CO) and,
# being a static asset path rather than UI copy, independent of display language.
_ZERO_RESULTS_ASSET_MARKER = "empty-state_overfiltering"
# Secondary, locale-text fallback — only the Spanish phrase below is confirmed live
# ("No hay ningún anuncio que coincida con los criterios de búsqueda"); the rest are
# reasonable same-meaning strings for other locales, NOT live-verified — kept as a
# fallback specifically because they're unverified, never promoted above the asset marker.
_ZERO_RESULTS_TEXT_MARKERS = (
    "no hay ningún anuncio que coincida",  # es — CONFIRMED live
    "no ads match your search",            # en — not live-verified
    "no results found",                    # en — not live-verified
)
_CHALLENGE_MARKERS = (
    "checkpoint", "security check", "verificación de seguridad",
    "confirm your identity", "confirma tu identidad", "captcha",
)


def _classify_content(html: str, md: str, ads: List[dict]) -> Tuple[str, str]:
    """Returns (status, reason). status is one of:
    "ok" (real ads parsed), "zero_results" (genuine, confirmed no matches),
    "likely_blocked" (neither — probably rate-limited or a bot challenge)."""
    if ads:
        return "ok", f"{len(ads)} structured ads parsed"
    lowered = (md or "").lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in lowered:
            return "likely_blocked", f"challenge/verification page detected ('{marker}')"
    if _ZERO_RESULTS_ASSET_MARKER in html:
        return "zero_results", f"genuine zero-results page detected (empty-state asset '{_ZERO_RESULTS_ASSET_MARKER}')"
    for marker in _ZERO_RESULTS_TEXT_MARKERS:
        if marker in lowered:
            return "zero_results", f"genuine zero-results page detected ('{marker}')"
    if len(html) < 20000:
        return "likely_blocked", f"near-empty response ({len(html)} bytes), no results marker found"
    return "likely_blocked", "no ads and no zero-results marker found in a full-size page — likely rate-limited"


# ---------------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------------

@mcp.tool(description="Search the public Facebook Ad Library for a keyword in a given country "
                      "(works for commercial ads, any country, any display language — no "
                      "token). Renders the SPA with a headless browser, scrolls past the "
                      "first page, and returns structured ad cards: advertiser, run date, "
                      "creative count, landing domain, CTA, headline, body copy, and image/"
                      "video URLs when present. Pass advertiser_page_id to pull every ad "
                      "from one Page (less reliable — see the tool's own warning field).")
def search_ad_library(
    query: str = "",
    country: str = "MX",
    active_status: str = "active",
    media_type: str = "all",
    ad_type: str = "all",
    advertiser_page_id: str = "",
    wait_seconds: int = 8,
    scroll_rounds: int = 8,
) -> dict:
    """
    Args:
        query: keyword(s) to search (advertiser name, product, angle...). Optional if
               advertiser_page_id is given.
        country: ISO country code the ads were delivered in (MX, US, ES, CO, ...)
        active_status: active | inactive | all
        ad_type: all | political_and_issue_ads | employment_ads | housing_ads | financial_products_and_services_ads
        media_type: all | image | meme | video | none
        advertiser_page_id: if set, target that Page's "all ads" view instead of a keyword
               search. This view is heavier client-rendered and does not always hydrate
               headless — a keyword search of the advertiser's name is the more reliable path.
        wait_seconds: how long to let the SPA hydrate before scraping (raise if results are
               empty). Capped at 30s.
        scroll_rounds: infinite-scroll cycles to load more ads past the first ~24
               (0 = first render only, fast; 8 default; capped at 20)
    """
    if not query and not advertiser_page_id:
        return {"success": False, "error": "Pass either query or advertiser_page_id."}

    params = {
        "active_status": active_status,
        "ad_type": ad_type,
        "country": country,
        "media_type": media_type,
    }
    if advertiser_page_id:
        params["view_all_page_id"] = advertiser_page_id
        params["search_type"] = "page"
        if query:
            params["q"] = query
    else:
        params["search_type"] = "keyword_unordered"
        params["q"] = query
    url = AD_LIBRARY_BASE + "?" + "&".join(f"{k}={quote(str(v))}" for k, v in params.items())

    t0 = time.monotonic()
    try:
        rendered = asyncio.run(_render_ad_library(url, wait_seconds, scroll_rounds))
    except Exception as e:
        return {"success": False, "error": str(e), "url": url}
    elapsed = round(time.monotonic() - t0, 1)

    if rendered["outcome"] == RenderOutcome.NETWORK_ERROR:
        logger.warning("search_ad_library network_error url=%s elapsed=%.1fs", url, elapsed)
        return {"success": False, "error_type": "network_error", "error": rendered["error"],
                "url": url, "elapsed_seconds": elapsed}

    html, md = rendered["html"], rendered["markdown"]
    ads = _extract_structured_ads(html)
    extraction_method = "json_island"
    if not ads:
        # Fallback path (§1) — only reached if the primary extractor found nothing.
        ads = _parse_ad_library_markdown_fallback(md)
        extraction_method = "markdown_fallback" if ads else "none"

    status, reason = _classify_content(html, md, ads)
    logger.info("search_ad_library status=%s ads=%d elapsed=%.1fs country=%s extraction=%s",
                status, len(ads), elapsed, country, extraction_method)

    if status == "zero_results":
        return {"success": True, "query": query or f"page_id:{advertiser_page_id}", "country": country,
                "url": url, "total_ads_parsed": 0, "ads": [], "note": reason,
                "elapsed_seconds": elapsed}

    if status == "likely_blocked":
        return {"success": False, "error_type": "likely_blocked", "url": url, "note": reason,
                "status_code": rendered["status_code"], "elapsed_seconds": elapsed,
                "markdown_preview": md[:1500]}

    advertisers: Dict[str, int] = {}
    for a in ads:
        name = a.get("advertiser") or "unknown"
        advertisers[name] = advertisers.get(name, 0) + 1

    return {
        "success": True,
        "query": query or f"page_id:{advertiser_page_id}",
        "country": country,
        "url": url,
        "scroll_rounds": min(max(0, int(scroll_rounds)), MAX_SCROLL_ROUNDS),
        "extraction_method": extraction_method,
        "total_ads_parsed": len(ads),
        "total_advertisers": len(advertisers),
        "advertisers": dict(sorted(advertisers.items(), key=lambda x: -x[1])),
        "ads": ads,
        "elapsed_seconds": elapsed,
    }


@mcp.tool(description="Scrape any Facebook Ad Library URL you already have (a prefilled search, "
                      "an advertiser's 'view all ads' page, a shared filter link) and return "
                      "structured ad cards plus the raw rendered text.")
def scrape_ad_library_url(url: str, wait_seconds: int = 8, scroll_rounds: int = 8) -> dict:
    """
    Args:
        url: a facebook.com/ads/library/... URL
        wait_seconds: SPA hydration wait before scraping (capped at 30s)
        scroll_rounds: infinite-scroll cycles to load more cards (0 = first render only,
               capped at 20)
    """
    if "facebook.com/ads/library" not in url:
        return {"success": False, "error": "URL must be a facebook.com/ads/library/... link"}
    t0 = time.monotonic()
    try:
        rendered = asyncio.run(_render_ad_library(url, wait_seconds, scroll_rounds))
    except Exception as e:
        return {"success": False, "error": str(e), "url": url}
    elapsed = round(time.monotonic() - t0, 1)

    if rendered["outcome"] == RenderOutcome.NETWORK_ERROR:
        return {"success": False, "error_type": "network_error", "error": rendered["error"],
                "url": url, "elapsed_seconds": elapsed}

    html, md = rendered["html"], rendered["markdown"]
    ads = _extract_structured_ads(html)
    extraction_method = "json_island"
    if not ads:
        ads = _parse_ad_library_markdown_fallback(md)
        extraction_method = "markdown_fallback" if ads else "none"

    status, reason = _classify_content(html, md, ads)
    if status == "likely_blocked":
        return {"success": False, "error_type": "likely_blocked", "url": url, "note": reason,
                "status_code": rendered["status_code"], "elapsed_seconds": elapsed}

    return {
        "success": True,
        "url": url,
        "scroll_rounds": min(max(0, int(scroll_rounds)), MAX_SCROLL_ROUNDS),
        "extraction_method": extraction_method,
        "total_ads_parsed": len(ads),
        "ads": ads,
        "elapsed_seconds": elapsed,
        "status_code": rendered["status_code"],
    }


if __name__ == "__main__":
    print("Facebook Ad Library MCP (UBORA fork) — scraping tools: search_ad_library, scrape_ad_library_url")
    mcp.run(transport="stdio")
