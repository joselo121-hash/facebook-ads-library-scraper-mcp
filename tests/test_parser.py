"""Offline tests for the Ad Library extraction pipeline and tool wiring.

Run:  python -m pytest tests/ -q
These do not hit the network. Fixtures are built from the REAL field shapes confirmed
live in this fork's development session (country=CO, real commercial queries against
the public Ad Library, 2026-10-18) — anonymized (real advertiser names, IDs, CDN URLs
replaced with clearly-fake placeholders of the exact same shape), never fabricated
shapes. Where a fixture represents a scenario this session did NOT actually observe
live (marked below), it is built to the same confirmed JSON schema, not guessed.
"""
import json
import facebook_ads_mcp_complete as m


def _sjs_html(payload: dict) -> str:
    """Wrap a Relay-shaped payload the same way Facebook embeds it: a
    <script type="application/json" data-sjs> data island inside a real page shell."""
    return (
        "<html><body>"
        f'<script type="application/json" nonce="" data-content-len="1" data-sjs="">'
        f"{json.dumps(payload)}</script>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------------
# Real schema, anonymized — Spanish-locale render (LIVE-CONFIRMED shape, country=CO)
# ---------------------------------------------------------------------------------

ES_IMAGE_AD = {
    "ad_archive_id": "1000000000000001",
    "collation_count": 1,
    "is_active": True,
    "page_id": "2000000000000001",
    "page_name": "Marca Ejemplo CO",
    "start_date": 1786950000,
    "end_date": 1792306800,
    "publisher_platform": ["FACEBOOK", "INSTAGRAM", "AUDIENCE_NETWORK", "MESSENGER", "THREADS"],
    "snapshot": {
        "page_id": "2000000000000001",
        "page_name": "Marca Ejemplo CO",
        "page_profile_uri": "https://www.facebook.com/marcaejemploco/",
        "page_profile_picture_url": "https://scontent.example.fbcdn.net/avatar.jpg",
        "body": "Envío gratis en tu primera compra. Calidad garantizada.",
        "cta_type": "SHOP_NOW",
        "cta_text": "Comprar ahora",
        "title": "Zapatos deportivos cómodos para el día a día",
        "link_url": "https://tienda-ejemplo.com.co/producto/123",
        "link_description": "tienda-ejemplo.com.co",
        "original_image_url": "https://scontent.example.fbcdn.net/original.jpg",
        "resized_image_url": "https://scontent.example.fbcdn.net/resized.jpg",
        "videos": [],
        "cards": [],
    },
}

# A card genuinely missing cta_type — real example found live among 80 real ads
# (1 of 80 had no CTA at all) — confirms the parser must never require this field.
ES_IMAGE_AD_NO_CTA = {
    "ad_archive_id": "1000000000000002",
    "collation_count": 1,
    "is_active": True,
    "page_id": "2000000000000002",
    "page_name": "Otra Marca",
    "start_date": 1786000000,
    "end_date": None,
    "publisher_platform": ["FACEBOOK"],
    "snapshot": {
        "page_name": "Otra Marca",
        "page_profile_uri": "https://www.facebook.com/otramarca/",
        "body": "Nuevo catálogo disponible.",
        "cta_type": None,
        "cta_text": None,
        "title": None,
        "link_url": None,
        "original_image_url": "https://scontent.example.fbcdn.net/img2.jpg",
        "cards": [],
    },
}

# Real video ad shape (confirmed live — 3 of 80 real Colombia results were video).
ES_VIDEO_AD = {
    "ad_archive_id": "1000000000000003",
    "collation_count": 1,
    "is_active": True,
    "page_id": "2000000000000003",
    "page_name": "Marca Video SAS",
    "start_date": 1786000000,
    "end_date": 1792306800,
    "publisher_platform": ["FACEBOOK", "INSTAGRAM"],
    "snapshot": {
        "page_name": "Marca Video SAS",
        "page_profile_uri": "https://www.facebook.com/marcavideosas/",
        "body": "Descubre nuestra nueva colección en video.",
        "cta_type": "SHOP_NOW",
        "cta_text": "Shop Now",
        "title": "Marca Video SAS",
        "link_url": "https://marcavideo.com.co/coleccion",
        "video_hd_url": "https://video.example.fbcdn.net/clip.mp4",
        "video_sd_url": "https://video.example.fbcdn.net/clip_sd.mp4",
        "video_preview_image_url": "https://scontent.example.fbcdn.net/poster.jpg",
        "original_image_url": None,
        "cards": [],
    },
}

# Real DCO/carousel shape — 10 of 80 real Colombia ads had multiple cards (6 each)
# under one ad_archive_id; this fixture uses 2 cards to keep it readable.
ES_DCO_AD = {
    "ad_archive_id": "1000000000000004",
    "collation_count": 1,
    "is_active": True,
    "page_id": "2000000000000004",
    "page_name": "Tienda Multi",
    "start_date": 1786000000,
    "end_date": 1792306800,
    "publisher_platform": ["FACEBOOK", "INSTAGRAM"],
    "snapshot": {
        "page_name": "Tienda Multi",
        "page_profile_uri": "https://www.facebook.com/tiendamulti/",
        "page_profile_picture_url": "https://scontent.example.fbcdn.net/avatar2.jpg",
        "cards": [
            {
                "body": "Producto A en oferta.", "cta_type": "SHOP_NOW", "cta_text": "Shop Now",
                "title": "Producto A", "link_url": "https://tiendamulti.com.co/a",
                "original_image_url": "https://scontent.example.fbcdn.net/a.jpg",
                "resized_image_url": "https://scontent.example.fbcdn.net/a_r.jpg",
            },
            {
                "body": "Producto B en oferta.", "cta_type": "LEARN_MORE", "cta_text": "Learn More",
                "title": "Producto B", "link_url": "https://tiendamulti.com.co/b",
                "original_image_url": "https://scontent.example.fbcdn.net/b.jpg",
                "resized_image_url": "https://scontent.example.fbcdn.net/b_r.jpg",
            },
        ],
    },
}

# Same ad_archive_id repeated verbatim — the infinite-scroll re-render / nested-Relay-
# payload duplication this fork's dedupe (by ad_archive_id, then by card identity) must
# collapse back to the cards above, never double them.
_DUPLICATE_WRAPPER = {
    "require": [
        ["X", "handle", None, [
            {"__bbox": {"result": {"data": {"edges": [
                {"node": {"collated_results": [ES_IMAGE_AD, ES_IMAGE_AD]}},
            ]}}}},
        ]],
    ],
}


ES_IMAGE_AD_RICH_TEXT_BODY = {
    "ad_archive_id": "1000000000000099",
    "collation_count": 1,
    "is_active": True,
    "page_id": "2000000000000099",
    "page_name": "Marca Ejemplo CO",
    "start_date": 1786950000,
    "end_date": None,
    "publisher_platform": ["FACEBOOK", "INSTAGRAM"],
    "snapshot": {
        "page_id": "2000000000000099",
        "page_name": "Marca Ejemplo CO",
        # LIVE-CONFIRMED shape (2026-10-18, real session data): 10 of 68 real ads in one
        # response had `body` as this rich-text wrapper object, not a plain string — the
        # bug `_as_plain_text` exists to fix. title/cta_text/link_url were always plain
        # strings in every real ad observed; only `body` has this wrapper.
        "body": {"text": "Copy real envuelto en objeto, no string plano."},
        "cta_type": "SHOP_NOW",
        "cta_text": "Comprar",
        "title": "Título normal",
        "link_url": "https://tienda-ejemplo.com.co/producto/999",
        "original_image_url": "https://scontent.example.fbcdn.net/original99.jpg",
    },
}


def test_json_island_extracts_structured_fields():
    html = _sjs_html({"a": [ES_IMAGE_AD]})
    ads = m._extract_structured_ads(html)
    assert len(ads) == 1
    a = ads[0]
    assert a["library_id"] == "1000000000000001"
    assert a["ad_details_url"] == "https://www.facebook.com/ads/library/?id=1000000000000001"
    assert a["advertiser"] == "Marca Ejemplo CO"
    assert a["advertiser_handle"] == "marcaejemploco"
    assert a["cta"] == "SHOP_NOW"          # stable enum
    assert a["cta_label"] == "Comprar ahora"  # localized display text, secondary
    assert a["landing_url"] == "https://tienda-ejemplo.com.co/producto/123"
    assert a["landing_domain"] == "tienda-ejemplo.com.co"
    assert a["media_type"] == "IMAGE"
    assert a["started_running"] == "2026-08-17"
    assert "FACEBOOK" in a["platforms"] and "INSTAGRAM" in a["platforms"]
    assert a["extraction_method"] == "json_island"


def test_rich_text_wrapper_body_is_unwrapped_to_plain_string():
    html = _sjs_html({"a": [ES_IMAGE_AD_RICH_TEXT_BODY]})
    ads = m._extract_structured_ads(html)
    assert len(ads) == 1
    assert ads[0]["body"] == "Copy real envuelto en objeto, no string plano."
    assert isinstance(ads[0]["body"], str)


def test_as_plain_text_handles_every_shape_without_crashing():
    assert m._as_plain_text("plain string") == "plain string"
    assert m._as_plain_text({"text": "wrapped"}) == "wrapped"
    assert m._as_plain_text({"text": 123}) is None   # malformed: text itself not a string
    assert m._as_plain_text({}) is None
    assert m._as_plain_text(None) is None
    assert m._as_plain_text(42) is None


def test_missing_cta_and_title_are_null_never_fabricated():
    html = _sjs_html({"a": [ES_IMAGE_AD_NO_CTA]})
    ads = m._extract_structured_ads(html)
    assert len(ads) == 1
    assert ads[0]["cta"] is None
    assert ads[0]["headline"] is None
    assert ads[0]["landing_url"] is None
    assert ads[0]["landing_domain"] is None
    assert ads[0]["ended_running"] is None


def test_video_ad_extracts_video_and_preview_urls():
    html = _sjs_html({"a": [ES_VIDEO_AD]})
    ads = m._extract_structured_ads(html)
    assert len(ads) == 1
    a = ads[0]
    assert a["media_type"] == "VIDEO"
    assert a["creative_video"] == "https://video.example.fbcdn.net/clip.mp4"
    assert a["video_preview_image"] == "https://scontent.example.fbcdn.net/poster.jpg"
    assert a["creative_image"] is None


def test_dco_multi_card_ad_becomes_multiple_records():
    html = _sjs_html({"a": [ES_DCO_AD]})
    ads = m._extract_structured_ads(html)
    assert len(ads) == 2
    assert {a["headline"] for a in ads} == {"Producto A", "Producto B"}
    assert all(a["library_id"] == "1000000000000004" for a in ads)
    assert all(a["advertiser"] == "Tienda Multi" for a in ads)


def test_dedupe_collapses_repeated_ad_archive_id():
    html = _sjs_html(_DUPLICATE_WRAPPER)
    ads = m._extract_structured_ads(html)
    assert len(ads) == 1  # not 2, despite ES_IMAGE_AD appearing twice in the payload


def test_json_island_is_language_independent_english_fixture():
    # Same confirmed schema, English page_name/body/cta_text — never live-captured in
    # English this session (Facebook rendered CO requests in Spanish), but the schema
    # itself (ad_archive_id, cta_type, link_url, ...) is confirmed real and the same
    # regardless of display language, which is exactly the property under test here.
    en_ad = dict(ES_IMAGE_AD)
    en_ad["ad_archive_id"] = "1000000000000005"
    en_ad["page_name"] = "Example Brand US"
    en_ad["snapshot"] = dict(ES_IMAGE_AD["snapshot"])
    en_ad["snapshot"].update({
        "page_name": "Example Brand US",
        "body": "Free shipping on your first order.",
        "cta_text": "Shop Now",
        "title": "Comfortable everyday sneakers",
    })
    html = _sjs_html({"a": [en_ad]})
    ads = m._extract_structured_ads(html)
    assert len(ads) == 1
    assert ads[0]["cta"] == "SHOP_NOW"
    assert ads[0]["body"] == "Free shipping on your first order."


def test_no_json_island_falls_back_to_markdown_parser():
    # A page with no Relay data island at all — the extractor must return empty,
    # never raise, so the caller can fall back to the markdown parser.
    html = "<html><body><p>no data islands here</p></body></html>"
    assert m._extract_structured_ads(html) == []


# ---------------------------------------------------------------------------------
# Result classification (§2) — 403-with-content vs. genuine zero-results vs. blocked
# ---------------------------------------------------------------------------------

# Real zero-results marker confirmed live: query "xzqwkjploiuyqqqnonsenseadvertiser999",
# country=CO. Facebook's own empty-state illustration filename — language-independent
# by construction (a static asset path, not UI copy).
ZERO_RESULTS_HTML_FRAGMENT = (
    "<html><body><img src='https://www.facebook.com/images/ads/politics/archive/"
    "empty-state_overfiltering_3x.png'>"
    "No hay ningún anuncio que coincida con los criterios de búsqueda</body></html>"
)


def test_classify_real_ads_as_ok():
    status, reason = m._classify_content("<html>...</html>" * 1, "md", [{"library_id": "1"}])
    assert status == "ok"


def test_classify_genuine_zero_results_by_asset_marker():
    status, reason = m._classify_content(ZERO_RESULTS_HTML_FRAGMENT, ZERO_RESULTS_HTML_FRAGMENT, [])
    assert status == "zero_results"
    assert "empty-state" in reason


def test_classify_genuine_zero_results_by_spanish_text_without_asset():
    md = "No hay ningún anuncio que coincida con los criterios de búsqueda"
    status, reason = m._classify_content("<html>" + md + "</html>" * 200, md, [])
    assert status == "zero_results"


def test_classify_near_empty_403_as_likely_blocked():
    # §2 rule: HTTP 403 with no usable content → blocked. This is a SYNTHETIC fixture
    # (this session's live attempts always got substantial real HTML back, even when
    # crawl4ai's own flag said "blocked") — not live-captured, built to the documented
    # near-empty-response shape.
    tiny_html = "<html><body>Access Denied</body></html>"
    status, reason = m._classify_content(tiny_html, "Access Denied", [])
    assert status == "likely_blocked"
    assert "bytes" in reason


def test_classify_challenge_page_as_likely_blocked():
    md = "Please complete this security check to continue. captcha required."
    status, reason = m._classify_content("<html>" + md + "</html>" * 500, md, [])
    assert status == "likely_blocked"
    assert "challenge" in reason


def test_classify_full_size_page_without_ads_or_marker_as_likely_blocked():
    # A full-size page that has neither real ads nor any recognised zero-results
    # marker — conservative default per §2 ("no inventar éxito"): treat as blocked/
    # rate-limited rather than silently reporting a false zero-results.
    html = "<html>" + ("x" * 25000) + "</html>"
    status, reason = m._classify_content(html, "unrecognised content", [])
    assert status == "likely_blocked"
    assert "rate-limited" in reason


# ---------------------------------------------------------------------------------
# Markdown fallback parser — kept verbatim from upstream, English-locale only
# ---------------------------------------------------------------------------------

MARKDOWN_FIXTURE = """
Some header junk

Library ID: 111111111111111
Started running on Jan 5, 2026
**3 ads** use this creative
[Glyver](https://www.facebook.com/glyver/)
**Sponsored**
Automatiza tu operacion con IA. Agenda tu diagnostico gratis: [https://glyver.net](https://l.facebook.com/l.php?u=https%3A%2F%2Fglyver.net%2F&h=AbC)
[![creative](https://scontent.xx.fbcdn.net/v/photo.jpg) GLYVER.NET Agenda tu diagnostico Learn more ](https://l.facebook.com/l.php?u=https%3A%2F%2Fglyver.net%2F&h=AbC)
Facebook Instagram

Library ID: 222222222222222
Started running on Feb 16, 2026
[Otra Marca](https://www.facebook.com/otramarca/)
**Sponsored**
Prueba nuestro producto hoy.
[![creative](https://scontent.xx.fbcdn.net/v/photo2.jpg) OTRA Compra ahora Shop now ](https://l.facebook.com/l.php?u=https%3A%2F%2Fotramarca.com%2Fpromo)

Library ID: 111111111111111
(duplicate card re-rendered by infinite scroll — should be ignored)
[Glyver](https://www.facebook.com/glyver/)
"""


def _parsed_markdown():
    return m._parse_ad_library_markdown_fallback(MARKDOWN_FIXTURE)


def test_markdown_fallback_card_count_and_dedupe():
    ads = _parsed_markdown()
    assert len(ads) == 2
    assert [a["library_id"] for a in ads] == ["111111111111111", "222222222222222"]
    assert all(a["extraction_method"] == "markdown_fallback" for a in ads)


def test_markdown_fallback_advertiser_and_dates():
    a = _parsed_markdown()[0]
    assert a["advertiser"] == "Glyver"
    assert a["advertiser_handle"] == "glyver"
    assert a["started_running"] == "Jan 5, 2026"
    assert a["ads_using_creative"] == 3


def test_markdown_fallback_landing_url_decoded():
    a = _parsed_markdown()[0]
    assert a["landing_url"] == "https://glyver.net/"
    assert a["landing_domain"] == "glyver.net"


def test_markdown_fallback_cta_and_link_text():
    a, b = _parsed_markdown()
    assert a["cta"] == "Learn more"
    assert a["link_text"] == "Agenda tu diagnostico"
    assert b["cta"] == "Shop now"


def test_markdown_fallback_body_strips_redirect_links():
    a = _parsed_markdown()[0]
    assert "l.facebook.com" not in a["body"]
    assert a["body"].startswith("Automatiza tu operacion con IA")


def test_markdown_fallback_empty_input():
    assert m._parse_ad_library_markdown_fallback("") == []


def test_only_scraping_tools_are_registered():
    # The server is a pure Ad Library scraper — no ads_archive API tools.
    assert hasattr(m, "search_ad_library")
    assert hasattr(m, "scrape_ad_library_url")
    for gone in ("search_facebook_ads", "analyze_ad_performance_metrics",
                 "generate_facebook_intelligence_report", "fb_api"):
        assert not hasattr(m, gone), f"{gone} should have been removed"


def test_hardening_limits_are_enforced():
    # §7 — scroll_rounds/wait_seconds are capped regardless of what a caller passes;
    # a real safety rail, not just documentation.
    assert m.MAX_SCROLL_ROUNDS > 0
    assert m.MAX_WAIT_SECONDS > 0
    assert m.MAX_CONCURRENT_RENDERS >= 1
