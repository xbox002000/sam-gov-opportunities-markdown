"""Markdown notice cards + REPORT.md. Cards never include POC email/phone lists."""
from __future__ import annotations

import json
from typing import Any

from sam_api import scrub_text


def _fmt(v: Any) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.2f}" if v != int(v) else f"{int(v):,}"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) if v else "—"
    return str(v)


def _esc(s: Any) -> str:
    return _fmt(s).replace("|", "\\|").replace("\n", " ")


def _link_text(s: str | None) -> str:
    return (s or "").replace("[", "(").replace("]", ")").replace("\n", " ").strip()


def money_usd(amount: float | None) -> str | None:
    if amount is None:
        return None
    if amount == int(amount):
        return f"${int(amount):,}"
    return f"${amount:,.2f}"


def build_card(item: dict) -> str:
    """One Markdown notice card (no POC contacts)."""
    title = _link_text(item.get("title")) or "(untitled notice)"
    lines = [f"### {title}", ""]
    nid = item.get("noticeId")
    sol = item.get("solicitationNumber")
    link = f" · [view on SAM.gov]({item['uiLink']})" if item.get("uiLink") else ""
    lines.append(f"- **Agency:** {_esc(item.get('agency')) or '—'}")
    id_bits = []
    if nid:
        id_bits.append(f"notice `{nid}`")
    if sol:
        id_bits.append(f"sol `{sol}`")
    lines.append(f"- **IDs:** " + (" · ".join(id_bits) if id_bits else "—") + link)
    if item.get("type") or item.get("baseType"):
        t = item.get("type") or ""
        bt = item.get("baseType")
        lines.append(f"- **Type:** {_esc(t)}" + (f" (base: {_esc(bt)})" if bt and bt != t else ""))
    if item.get("naicsCode"):
        lines.append(f"- **NAICS:** `{item['naicsCode']}`" + (
            f" · classification `{item['classificationCode']}`" if item.get("classificationCode") else ""
        ))
    elif item.get("classificationCode"):
        lines.append(f"- **Classification:** `{item['classificationCode']}`")
    if item.get("setAside") or item.get("setAsideCode"):
        lines.append(
            f"- **Set-aside:** {_esc(item.get('setAside') or item.get('setAsideCode'))}"
            + (f" (`{item['setAsideCode']}`)" if item.get("setAside") and item.get("setAsideCode") else "")
        )
    dates = []
    if item.get("postedDate"):
        dates.append(f"posted {_esc(item['postedDate'])}")
    if item.get("responseDeadline"):
        dates.append(f"**deadline {_esc(item['responseDeadline'])}**")
    if item.get("archiveDate"):
        dates.append(f"archive {_esc(item['archiveDate'])}")
    if dates:
        lines.append(f"- **Dates:** " + " · ".join(dates))
    if item.get("active"):
        lines.append(f"- **Active:** {_esc(item.get('active'))}")
    amt = money_usd(item.get("awardAmount"))
    if amt or item.get("awardNumber") or item.get("awardeeName"):
        bits = [x for x in [amt, item.get("awardNumber"), item.get("awardeeName")] if x]
        lines.append(f"- **Award / value:** " + " · ".join(_esc(b) for b in bits)
                     + (f" ({_esc(item.get('awardDate'))})" if item.get("awardDate") else ""))
    pop = item.get("placeOfPerformance") or {}
    if pop:
        loc = ", ".join(str(pop[k]) for k in ("city", "state", "zip", "country") if pop.get(k))
        if loc:
            lines.append(f"- **Place of performance:** {_esc(loc)}")
    if item.get("descriptionExcerpt"):
        excerpt = scrub_text(item["descriptionExcerpt"]) or ""
        lines += ["", "> " + excerpt.replace("\n", "\n> ")]
    # Explicit: never add POC / email / phone sections
    return "\n".join(lines).rstrip() + "\n"


def build_markdown(rows: list[dict], summary: dict) -> str:
    ok = [r for r in rows if r.get("status") == "ok"]
    bad = [r for r in rows if r.get("status") != "ok"]
    L: list[str] = []
    L.append("# SAM.gov opportunities — Markdown notice cards")
    L.append("")
    L.append(f"- Generated: {summary.get('finishedAt')}")
    f = summary.get("filters") or {}
    L.append(
        f"- Window: {f.get('postedFrom') or '—'} → {f.get('postedTo') or '—'} · "
        f"maxNotices {summary.get('maxNotices', '—')}"
    )
    filt_bits = [f"{k}={v}" for k, v in f.items()
                 if k in ("keywords", "ncode", "ptype", "organizationName", "solnum", "state", "noticeIds") and v]
    if filt_bits:
        L.append("- Filters: " + "; ".join(filt_bits))
    L.append(f"- Notices OK: **{len(ok)}** · failed / empty (not charged): **{len(bad)}**")
    L.append(f"- API key supplied: {'yes' if summary.get('apiKeyProvided') else 'no'}")
    if summary.get("totalRecords"):
        L.append(f"- SAM totalRecords (first page): {summary.get('totalRecords')}")
    if summary.get("charged"):
        L.append(f"- Charged events: `{summary.get('charged')}`")
    L.append("- Data: official SAM.gov Opportunities API v2 only. Point-of-contact emails and phones are never included.")
    L.append("")
    if summary.get("keyHint") and not ok:
        L.append(f"> **Key required:** {summary['keyHint']}")
        L.append("")
    if ok:
        L.append("| # | Title | Agency | NAICS | Posted | Deadline | Value |")
        L.append("|---|---|---|---|---|---|---|")
        for i, r in enumerate(ok, 1):
            L.append(
                f"| {i} | {_esc((r.get('title') or '')[:80])} | {_esc((r.get('agency') or '')[:40])} | "
                f"{_esc(r.get('naicsCode'))} | {_esc(r.get('postedDate'))} | "
                f"{_esc(r.get('responseDeadline'))} | {_esc(money_usd(r.get('awardAmount')) or '—')} |"
            )
        L.append("")
        for r in ok:
            L.append(r.get("markdown") or build_card(r))
            L.append("---")
            L.append("")
    if bad:
        L.append("## Not charged (failures / empty)")
        L.append("")
        L.append("| noticeId | errorClass | HTTP | hint |")
        L.append("|---|---|---|---|")
        for r in bad:
            L.append(
                f"| {_esc(r.get('noticeId'))} | `{_esc(r.get('errorClass'))}` | "
                f"{_esc(r.get('httpStatus'))} | {_esc((r.get('hint') or r.get('errorMessage') or '')[:120])} |"
            )
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def build_report_json(rows: list[dict], summary: dict) -> str:
    payload = {
        "summary": {
            "finishedAt": summary.get("finishedAt"),
            "noticesOk": summary.get("noticesOk"),
            "noticesFailed": summary.get("noticesFailed"),
            "byClass": summary.get("byClass"),
            "apiKeyProvided": summary.get("apiKeyProvided"),
            "charged": summary.get("charged"),
            "filters": summary.get("filters"),
        },
        "notices": rows,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
