"""Apify Actor: SAM.gov contract opportunities (official API v2) → Markdown notice cards.

- Official HTTP only: https://api.sam.gov/opportunities/v2/search (+ optional noticedesc).
- Buyer MUST supply public API key (`samApiKey`). No key → SAM returns HTTP 404; free.
- No HTML scrape of sam.gov. No contacts / leads / email harvesting — POC blocks dropped.
- Pay-per-event: `opportunity-notice` per successful notice card. Failures free.
- Adjacent to uk-tenders-ocds-markdown (US federal vs UK OCDS); not a redo.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import resource
import time
from collections import Counter

import httpx
from apify import Actor

from charging import Charger
from report import build_card, build_markdown, build_report_json
from sam_api import (
    KEY_HINT, UA, NoticeResult, build_search_params, fail_item, fetch_description,
    flatten_notice, resolve_posted_range, search_page, scrub_text, strip_contacts,
)

EVENT = "opportunity-notice"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _int(inp: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = inp.get(key)
    try:
        v = int(default if v is None or v == "" else v)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.replace("\n", ",").split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def resolve_options(inp: dict) -> dict:
    days_back = inp.get("daysBack")
    try:
        days_back = int(days_back) if days_back not in (None, "") else 7
    except (TypeError, ValueError):
        days_back = 7
    d_from, d_to = resolve_posted_range(inp.get("postedFrom"), inp.get("postedTo"), days_back)
    notice_ids = list(dict.fromkeys(_list(inp.get("noticeIds"))))
    ptype = inp.get("ptype")
    if isinstance(ptype, list):
        ptype = ",".join(str(x).strip() for x in ptype if str(x).strip()) or None
    elif ptype is not None:
        ptype = str(ptype).strip() or None
    return {
        "posted_from": d_from,
        "posted_to": d_to,
        "keywords": (str(inp.get("keywords") or "").strip()) or None,
        "ncode": (str(inp.get("ncode") or inp.get("naics") or "").strip()) or None,
        "ptype": ptype,
        "organization_name": (str(inp.get("organizationName") or "").strip()) or None,
        "solnum": (str(inp.get("solnum") or "").strip()) or None,
        "notice_ids": notice_ids,
        "state": (str(inp.get("state") or "").strip()) or None,
        "type_of_set_aside": (str(inp.get("typeOfSetAside") or "").strip()) or None,
        "status": (str(inp.get("status") or "").strip()) or None,
        "max_notices": _int(inp, "maxNotices", 50, 1, 1000),
        "page_size": _int(inp, "pageSize", 100, 1, 1000),
        "conc": _int(inp, "maxConcurrency", 2, 1, 5),
        "max_retries": _int(inp, "maxRetries", 3, 0, 6),
        "timeout": float(_int(inp, "requestTimeoutSecs", 30, 5, 120)),
        "desc_chars": _int(inp, "descriptionMaxChars", 800, 0, 5000),
        "fetch_descriptions": bool(inp.get("fetchDescriptions", True)),
    }


async def main() -> None:
    async with Actor:
        t0 = time.time()
        inp = await Actor.get_input() or {}
        opt = resolve_options(inp)
        charger = Charger(EVENT)

        api_key = (inp.get("samApiKey") or "").strip() or None
        key_source = "input" if api_key else None
        if not api_key:
            for env_name in ("SAM_API_KEY", "SAM_GOV_API_KEY"):
                env_key = os.getenv(env_name, "").strip()
                if env_key:
                    api_key, key_source = env_key, f"env:{env_name}"
                    break

        if not api_key:
            Actor.log.warning(
                "No samApiKey supplied. SAM.gov Opportunities API returns HTTP 404 without a public "
                "api_key. Those rows are marked failed and NOT charged. " + KEY_HINT
            )
        else:
            Actor.log.info(f"Using SAM.gov public API key from {key_source} (value hidden).")

        filters_echo = {
            "postedFrom": opt["posted_from"].strftime("%m/%d/%Y"),
            "postedTo": opt["posted_to"].strftime("%m/%d/%Y"),
            "keywords": opt["keywords"],
            "ncode": opt["ncode"],
            "ptype": opt["ptype"],
            "organizationName": opt["organization_name"],
            "solnum": opt["solnum"],
            "state": opt["state"],
            "noticeIds": opt["notice_ids"] or None,
            "status": opt["status"],
        }
        Actor.log.info(
            f"Window {filters_echo['postedFrom']}→{filters_echo['postedTo']}; "
            f"maxNotices={opt['max_notices']} concurrency={opt['conc']}"
        )
        await Actor.set_status_message("Searching SAM.gov Opportunities API…")

        headers = {"User-Agent": UA, "Accept": "application/json"}
        rows: list[dict] = []
        cls_counts: Counter[str] = Counter()
        state = {"ok": 0, "fail": 0, "skipped": 0, "pages": 0, "scanned": 0, "total_records": 0}
        key_hint: str | None = None

        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=opt["conc"] + 2),
        ) as client:
            # --- Path A: explicit noticeId list (one search per id) ---
            if opt["notice_ids"]:
                sem = asyncio.Semaphore(opt["conc"])
                lock = asyncio.Lock()

                async def one_notice(nid: str) -> None:
                    nonlocal key_hint
                    async with sem:
                        async with lock:
                            if charger.limit_reached or state["ok"] >= opt["max_notices"]:
                                state["skipped"] += 1
                                return
                        params = build_search_params(
                            posted_from=opt["posted_from"], posted_to=opt["posted_to"],
                            limit=1, offset=0, notice_id=nid,
                        )
                        page = await search_page(
                            client, api_key=api_key, params=params,
                            max_retries=opt["max_retries"], timeout=opt["timeout"],
                        )
                        async with lock:
                            state["pages"] += 1
                        if not page.ok or not page.opportunities:
                            ec = page.error_class or "not_found"
                            item = fail_item(
                                notice_id=nid, error_class=ec,
                                error_message=page.error_message, http_status=page.status,
                                attempts=page.attempts, hint=page.hint,
                            )
                            async with lock:
                                if ec in ("missing_key", "api_key_invalid", "unauthorized"):
                                    key_hint = page.hint or KEY_HINT
                                state["fail"] += 1
                                cls_counts[ec] += 1
                                rows.append(item)
                                await charger.push_free(item)
                            return
                        raw = page.opportunities[0]
                        desc = None
                        if opt["fetch_descriptions"] and opt["desc_chars"] > 0:
                            desc = await fetch_description(
                                client, raw.get("noticeId") or nid, api_key=api_key,
                                max_chars=opt["desc_chars"], max_retries=min(2, opt["max_retries"]),
                                timeout=opt["timeout"],
                            )
                        item = flatten_notice(raw, description_excerpt=desc)
                        item["markdown"] = build_card(item)
                        item["status"] = "ok"
                        item["errorClass"] = None
                        item["errorMessage"] = None
                        item["hint"] = None
                        item["httpStatus"] = 200
                        item["attempts"] = page.attempts
                        item["checkedAt"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
                        item = strip_contacts(item)
                        async with lock:
                            if state["ok"] >= opt["max_notices"] or charger.limit_reached:
                                state["skipped"] += 1
                                return
                            n = await charger.push_and_charge([item], event=EVENT)
                            if n:
                                state["ok"] += 1
                                cls_counts["ok"] += 1
                                rows.append(item)
                            else:
                                state["skipped"] += 1

                await asyncio.gather(*(one_notice(n) for n in opt["notice_ids"][: opt["max_notices"]]))

            # --- Path B: search with pagination ---
            else:
                offset = 0
                page_size = min(opt["page_size"], opt["max_notices"])
                while state["ok"] < opt["max_notices"] and not charger.limit_reached:
                    want = min(page_size, opt["max_notices"] - state["ok"])
                    params = build_search_params(
                        posted_from=opt["posted_from"], posted_to=opt["posted_to"],
                        limit=want, offset=offset,
                        keywords=opt["keywords"], ncode=opt["ncode"], ptype=opt["ptype"],
                        organization_name=opt["organization_name"], solnum=opt["solnum"],
                        state=opt["state"], type_of_set_aside=opt["type_of_set_aside"],
                        status=opt["status"],
                    )
                    page = await search_page(
                        client, api_key=api_key, params=params,
                        max_retries=opt["max_retries"], timeout=opt["timeout"],
                    )
                    state["pages"] += 1
                    if not page.ok:
                        ec = page.error_class or "network"
                        if ec in ("missing_key", "api_key_invalid", "unauthorized"):
                            key_hint = page.hint or KEY_HINT
                        item = fail_item(
                            error_class=ec, error_message=page.error_message,
                            http_status=page.status, attempts=page.attempts, hint=page.hint,
                        )
                        state["fail"] += 1
                        cls_counts[ec] += 1
                        rows.append(item)
                        await charger.push_free(item)
                        Actor.log.warning(f"Search failed: {ec} HTTP {page.status} (free)")
                        break
                    if state["pages"] == 1:
                        state["total_records"] = page.total_records
                    if not page.opportunities:
                        if state["ok"] == 0 and state["fail"] == 0:
                            item = fail_item(
                                error_class="no_matches",
                                error_message="Search returned 0 opportunities for this window/filters.",
                                http_status=200, attempts=page.attempts,
                                hint="Widen postedFrom/postedTo or daysBack, or relax keywords/ncode/ptype.",
                            )
                            state["fail"] += 1
                            cls_counts["no_matches"] += 1
                            rows.append(item)
                            await charger.push_free(item)
                        break

                    # Optional description fetches with concurrency
                    sem = asyncio.Semaphore(opt["conc"])

                    async def enrich(raw: dict) -> dict:
                        nid = raw.get("noticeId")
                        desc = None
                        if opt["fetch_descriptions"] and opt["desc_chars"] > 0 and nid and api_key:
                            async with sem:
                                desc = await fetch_description(
                                    client, nid, api_key=api_key,
                                    max_chars=opt["desc_chars"],
                                    max_retries=min(2, opt["max_retries"]),
                                    timeout=opt["timeout"],
                                )
                        item = flatten_notice(raw, description_excerpt=desc)
                        item["markdown"] = build_card(item)
                        item["status"] = "ok"
                        item["errorClass"] = None
                        item["errorMessage"] = None
                        item["hint"] = None
                        item["httpStatus"] = 200
                        item["attempts"] = page.attempts
                        item["checkedAt"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
                        return strip_contacts(item)

                    batch = page.opportunities[:want]
                    state["scanned"] += len(batch)
                    items = await asyncio.gather(*(enrich(r) for r in batch))
                    for item in items:
                        if state["ok"] >= opt["max_notices"] or charger.limit_reached:
                            state["skipped"] += 1
                            continue
                        n = await charger.push_and_charge([item], event=EVENT)
                        if n:
                            state["ok"] += 1
                            cls_counts["ok"] += 1
                            rows.append(item)
                            Actor.log.info(
                                f"[{state['ok']}/{opt['max_notices']}] "
                                f"{item.get('noticeId')}: OK {(item.get('title') or '')[:60]}"
                            )
                        else:
                            state["skipped"] += 1

                    await Actor.set_status_message(
                        f"Page {state['pages']}: {state['ok']} notice(s) OK, {state['fail']} failed"
                    )
                    offset += len(page.opportunities)
                    if offset >= page.total_records or len(page.opportunities) < want:
                        break

        peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        if key_hint is None and (cls_counts.get("missing_key") or cls_counts.get("api_key_invalid")):
            key_hint = KEY_HINT
        summary = {
            "filters": filters_echo,
            "maxNotices": opt["max_notices"],
            "pages": state["pages"],
            "scanned": state["scanned"],
            "totalRecords": state["total_records"] or None,
            "noticesOk": state["ok"],
            "noticesFailed": state["fail"],
            "skipped": state["skipped"],
            "byClass": dict(cls_counts),
            "apiKeyProvided": bool(api_key),
            "keyHint": key_hint if (key_hint and not state["ok"]) else None,
            "charged": dict(charger.counts),
            "spendingLimitReached": charger.limit_reached,
            "durationSecs": round(time.time() - t0, 1),
            "peakMemoryMb": peak_mb,
            "finishedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "source": "https://api.sam.gov/opportunities/v2/search",
        }
        store = await Actor.open_key_value_store()
        await store.set_value("REPORT.md", build_markdown(rows, summary), content_type="text/markdown; charset=utf-8")
        await store.set_value("REPORT.json", build_report_json(rows, summary), content_type="application/json; charset=utf-8")
        await store.set_value("SUMMARY", summary)
        await store.set_value("OUTPUT", summary)
        msg = (
            f"Done: {state['ok']} notice(s) OK, {state['fail']} failed (free), "
            f"{state['skipped']} skipped. Charged: {charger.counts or 'nothing'}."
        )
        if key_hint and not state["ok"]:
            msg += " Supply samApiKey (SAM.gov returned 404/missing key)."
        Actor.log.info(msg + f" Peak memory {peak_mb} MB, {summary['durationSecs']} s.")
        await Actor.set_status_message(msg, is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
