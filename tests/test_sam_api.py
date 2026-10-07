"""Unit + mock-transport tests for SAM.gov Opportunities → Markdown (no live network)."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx
import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import sam_api  # noqa: E402
from report import build_card, build_markdown  # noqa: E402

FX = lambda n: json.load(open(os.path.join(HERE, "fixtures", n)))  # noqa: E731


def run(coro):
    return asyncio.run(coro)


def test_title_length_and_slug():
    import json as _j
    actor = _j.load(open(os.path.join(HERE, "..", ".actor", "actor.json")))
    assert actor["name"] == "sam-gov-opportunities-markdown"
    assert len(actor["title"]) <= 63
    assert actor["defaultRunOptions"]["memoryMbytes"] == 256
    assert actor["defaultRunOptions"]["timeoutSecs"] == 3600
    assert set(actor["categories"]) == {"BUSINESS", "DEVELOPER_TOOLS", "AUTOMATION"}
    ppe = _j.load(open(os.path.join(HERE, "..", ".actor", "pay_per_event.json")))
    assert ppe["apify-actor-start"]["eventPriceUsd"] == 0.001
    assert ppe["opportunity-notice"]["eventPriceUsd"] == 0.003
    assert ppe["opportunity-notice"]["isPrimaryEvent"] is True


def test_resolve_posted_range_days_back():
    d_from, d_to = sam_api.resolve_posted_range(None, "10/05/2026", 7)
    assert d_to.isoformat() == "2026-10-05"
    assert d_from.isoformat() == "2026-09-28"
    d_from, d_to = sam_api.resolve_posted_range("01/01/2026", "06/01/2026", None)
    assert (d_to - d_from).days == 151
    with pytest.raises(ValueError, match="1 year"):
        sam_api.resolve_posted_range("01/01/2025", "01/02/2026", None)
    with pytest.raises(ValueError, match="after"):
        sam_api.resolve_posted_range("10/05/2026", "10/01/2026", None)


def test_strip_contacts_drops_poc():
    raw = FX("notice_award.json")
    assert raw["pointOfContact"][0]["email"] == "jesse.jones@gsa.gov"
    clean = sam_api.strip_contacts(raw)
    assert "pointOfContact" not in clean
    blob = json.dumps(clean)
    assert "jesse.jones@gsa.gov" not in blob
    assert "2174941263" not in blob


def test_scrub_text_emails_phones():
    s = sam_api.scrub_text("Call jane.doe@agency.gov or (202) 555-0199 today")
    assert "jane.doe@agency.gov" not in s
    assert "[email removed]" in s
    assert "[phone removed]" in s


def test_flatten_notice_no_contacts_and_award():
    item = sam_api.flatten_notice(FX("notice_award.json"), description_excerpt="Office renovation. Call 202-555-0100.")
    assert item["noticeId"] == "5b345bbb7127b91a3ad577b203fc6f68"
    assert item["title"].startswith("Historic Office")
    assert "GENERAL SERVICES" in item["agency"]
    assert item["naicsCode"] == "236220"
    assert item["awardAmount"] == 800620.0
    assert item["awardeeName"] == "D.G. Beyer, Inc."
    assert item["uiLink"].startswith("https://sam.gov/opp/")
    assert "pointOfContact" not in item
    assert "[phone removed]" in (item["descriptionExcerpt"] or "")
    blob = json.dumps(item)
    assert "jesse.jones" not in blob
    assert "@gsa.gov" not in blob


def test_build_card_has_no_poc_section():
    item = sam_api.flatten_notice(FX("notice_award.json"))
    md = build_card(item)
    assert "Historic Office Renovation" in md
    assert "$800,620" in md or "800,620" in md
    assert "jesse" not in md.lower()
    assert "@" not in md
    assert "point of contact" not in md.lower()
    assert "POC" not in md


def test_search_ok_with_key_mock():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        assert "api_key=SECRETKEY" in str(req.url)
        assert "postedFrom=" in str(req.url)
        return httpx.Response(200, json=FX("search_ok.json"))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            # Override base for this test
            old = sam_api.SAM_BASE
            sam_api.SAM_BASE = "https://api.sam.gov/opportunities/v2/search"
            try:
                params = sam_api.build_search_params(
                    posted_from=sam_api.parse_mmddyyyy("10/01/2026"),
                    posted_to=sam_api.parse_mmddyyyy("10/05/2026"),
                    limit=2, offset=0, keywords="cloud", ptype="o,k",
                )
                return await sam_api.search_page(c, api_key="SECRETKEY", params=params, max_retries=0, timeout=5)
            finally:
                sam_api.SAM_BASE = old

    page = run(go())
    assert page.ok and page.total_records == 2
    assert len(page.opportunities) == 2
    assert calls and "title=cloud" in calls[0]


def test_missing_key_404_free_class():
    def handler(req: httpx.Request) -> httpx.Response:
        assert "api_key=" not in str(req.url) or "api_key=&" in str(req.url) or str(req.url).endswith("api_key=")
        return httpx.Response(404, json=FX("error_404_nokey.json"))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            params = sam_api.build_search_params(
                posted_from=sam_api.parse_mmddyyyy("10/01/2026"),
                posted_to=sam_api.parse_mmddyyyy("10/05/2026"),
                limit=1, offset=0,
            )
            return await sam_api.search_page(c, api_key=None, params=params, max_retries=0, timeout=5)

    page = run(go())
    assert not page.ok
    assert page.error_class == "missing_key"
    assert page.status == 404
    assert "sam.gov" in (page.hint or "").lower()
    assert "Public API Key" in (page.hint or "")


def test_invalid_key_404():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=FX("error_404_badkey.json"))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            params = sam_api.build_search_params(
                posted_from=sam_api.parse_mmddyyyy("10/01/2026"),
                posted_to=sam_api.parse_mmddyyyy("10/05/2026"),
                limit=1, offset=0,
            )
            return await sam_api.search_page(c, api_key="BAD", params=params, max_retries=0, timeout=5)

    page = run(go())
    assert not page.ok
    assert page.error_class == "api_key_invalid"


def test_429_retries_then_fails():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json=FX("error_429.json"), headers={"Retry-After": "0"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            params = sam_api.build_search_params(
                posted_from=sam_api.parse_mmddyyyy("10/01/2026"),
                posted_to=sam_api.parse_mmddyyyy("10/05/2026"),
                limit=1, offset=0,
            )
            return await sam_api.search_page(c, api_key="K", params=params, max_retries=2, timeout=5)

    page = run(go())
    assert not page.ok and page.error_class == "rate_limited"
    assert len(calls) == 3  # initial + 2 retries


def test_fetch_description_scrubs_contacts():
    def handler(req: httpx.Request) -> httpx.Response:
        assert "api_key=K" in str(req.url)
        assert "noticeid=" in str(req.url)
        return httpx.Response(200, json=FX("noticedesc.json"))

    async def go():
        old = sam_api.SAM_DESC_BASE
        sam_api.SAM_DESC_BASE = "https://api.sam.gov/prod/opportunities/v1/noticedesc"
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await sam_api.fetch_description(
                    c, "5b345bbb7127b91a3ad577b203fc6f68", api_key="K", max_chars=200, max_retries=0, timeout=5,
                )
        finally:
            sam_api.SAM_DESC_BASE = old

    text = run(go())
    assert text
    assert "jane.doe@agency.gov" not in text
    assert "[email removed]" in text
    assert "[phone removed]" in text


def test_empty_search_ok_page():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=FX("search_empty.json"))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            params = sam_api.build_search_params(
                posted_from=sam_api.parse_mmddyyyy("10/01/2026"),
                posted_to=sam_api.parse_mmddyyyy("10/05/2026"),
                limit=10, offset=0,
            )
            return await sam_api.search_page(c, api_key="K", params=params, max_retries=0, timeout=5)

    page = run(go())
    assert page.ok and page.total_records == 0 and page.opportunities == []


def test_report_markdown_summary_and_no_leak():
    items = []
    for raw in FX("search_ok.json")["opportunitiesData"]:
        it = sam_api.flatten_notice(raw)
        it["markdown"] = build_card(it)
        it["status"] = "ok"
        items.append(it)
    fail = sam_api.fail_item(error_class="missing_key", http_status=404, hint=sam_api.KEY_HINT)
    rows = items + [fail]
    summary = {
        "finishedAt": "2026-10-05T00:00:00+00:00",
        "filters": {"postedFrom": "10/01/2026", "postedTo": "10/05/2026", "keywords": "cloud"},
        "maxNotices": 50,
        "noticesOk": 2,
        "noticesFailed": 1,
        "apiKeyProvided": True,
        "charged": {"opportunity-notice": 2},
        "keyHint": None,
    }
    md = build_markdown(rows, summary)
    assert "SAM.gov opportunities" in md
    assert "Historic Office" in md
    assert "Not charged" in md
    assert "missing_key" in md
    assert "jesse.jones" not in md
    assert "leak@example.com" not in md
    # title had email — must be scrubbed in flatten
    assert "jesse.jones@gsa.gov" not in md
    assert "@gsa.gov" not in md or "[email removed]" in md


def test_key_never_logged_in_error_text():
    def handler(req: httpx.Request) -> httpx.Response:
        # echo key in body to ensure redaction
        return httpx.Response(500, text="boom SUPERSECRETKEY boom")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            params = sam_api.build_search_params(
                posted_from=sam_api.parse_mmddyyyy("10/01/2026"),
                posted_to=sam_api.parse_mmddyyyy("10/05/2026"),
                limit=1, offset=0,
            )
            return await sam_api.search_page(c, api_key="SUPERSECRETKEY", params=params, max_retries=0, timeout=5)

    page = run(go())
    assert not page.ok
    assert "SUPERSECRETKEY" not in (page.error_message or "")
    assert "***" in (page.error_message or "")


def test_e2e_mock_charges_at_least_one_notice(tmp_path, monkeypatch):
    """Mock E2E: search returns ≥1 notice; flatten+card succeed (charge path simulated by counting ok items)."""
    opps = FX("search_ok.json")["opportunitiesData"]
    assert len(opps) >= 1
    charged = []
    for raw in opps:
        item = sam_api.flatten_notice(raw)
        item["markdown"] = build_card(item)
        item["status"] = "ok"
        # simulate PPE: only ok rows are charged
        charged.append(item)
        blob = json.dumps(item) + item["markdown"]
        assert "jesse.jones@gsa.gov" not in blob
        assert "leak@example.com" not in blob
        assert "pointOfContact" not in item
    assert len(charged) >= 1
    # Simulated charge: opportunity-notice × N
    price = 0.003
    assert len(charged) * price >= 0.003
