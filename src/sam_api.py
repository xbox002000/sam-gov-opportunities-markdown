"""SAM.gov Get Opportunities Public API v2 client (official HTTP only — no HTML scrape).

Endpoint: https://api.sam.gov/opportunities/v2/search
Auth: api_key query parameter (buyer-supplied public key from SAM.gov Account Details).
Never logs the key. Drops pointOfContact / email / phone fields from every parsed object.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

SAM_BASE = "https://api.sam.gov/opportunities/v2/search"
SAM_DESC_BASE = "https://api.sam.gov/prod/opportunities/v1/noticedesc"
_override = os.getenv("SAM_API_BASE_OVERRIDE", "")
if _override.startswith(("http://127.0.0.1", "http://localhost")):
    SAM_BASE = _override.rstrip("/")
_desc_override = os.getenv("SAM_DESC_BASE_OVERRIDE", "")
if _desc_override.startswith(("http://127.0.0.1", "http://localhost")):
    SAM_DESC_BASE = _desc_override.rstrip("/")

# Contact / PII keys that must NEVER appear in Actor output (drop recursively).
CONTACT_KEYS = frozenset({
    "email", "e_mail", "email_address", "emailAddress",
    "telephone", "telephone_number", "telephoneNumber", "phone", "phone_number",
    "phoneNumber", "mobile", "mobile_number", "mobileNumber", "fax", "fax_number",
    "contact_email", "contactEmail", "contact_telephone", "contactTelephone",
    "contact_number", "contactNumber", "pointOfContact", "pointofContact",
    "point_of_contact", "poc", "contacts",
})

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\w)"
)

KEY_HINT = (
    "SAM.gov Opportunities API requires a public API key (passed as api_key). "
    "Sign in at https://sam.gov/ → Account Details → Public API Key "
    "(docs: https://open.gsa.gov/api/get-opportunities-public-api/). "
    "Without a key the API returns HTTP 404. Failed rows are not charged. "
    "The publisher does not buy or host a SAM key — bring your own free public key."
)

UA = "Mozilla/5.0 (compatible; SamGovOpportunitiesMarkdown/0.1; +https://apify.com)"

# Procurement type codes → labels (official)
PTYPE_LABELS = {
    "u": "Justification (J&A)",
    "p": "Presolicitation",
    "a": "Award Notice",
    "r": "Sources Sought",
    "s": "Special Notice",
    "o": "Solicitation",
    "g": "Sale of Surplus Property",
    "k": "Combined Synopsis/Solicitation",
    "i": "Intent to Bundle Requirements (DoD-Funded)",
}


@dataclass
class SearchPage:
    ok: bool
    total_records: int = 0
    opportunities: list[dict] = field(default_factory=list)
    status: int = 0
    attempts: int = 0
    error_class: str | None = None
    error_message: str | None = None
    hint: str | None = None


@dataclass
class NoticeResult:
    ok: bool
    item: dict[str, Any]
    attempts: int = 0


def redact_secret(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret and text else text


def scrub_text(text: str | None) -> str | None:
    """Replace emails/phones in free text with placeholders (never harvest contacts)."""
    if not text:
        return text
    out = EMAIL_RE.sub("[email removed]", text)
    out = PHONE_RE.sub("[phone removed]", out)
    return out


def strip_contacts(obj: Any) -> Any:
    """Recursively drop contact / POC keys from dicts/lists."""
    if isinstance(obj, dict):
        return {k: strip_contacts(v) for k, v in obj.items() if k not in CONTACT_KEYS}
    if isinstance(obj, list):
        return [strip_contacts(v) for v in obj]
    return obj


def classify_error(status: int, body_text: str = "", *, has_key: bool = True) -> str:
    low = (body_text or "").lower()
    if status == 404:
        if (not has_key) or "no api_key" in low or "api_key was supplied" in low or "invalid api_key" in low:
            return "missing_key" if not has_key or "no api_key" in low else "api_key_invalid"
        return "not_found"
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    if status == 400:
        return "bad_request"
    if status >= 500:
        return "sam_server_error"
    if "invalid" in low and "api_key" in low:
        return "api_key_invalid"
    if "no api_key" in low:
        return "missing_key"
    return f"http_{status}" if status else "network"


def parse_mmddyyyy(s: str | None) -> dt.date | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def format_mmddyyyy(d: dt.date) -> str:
    return d.strftime("%m/%d/%Y")


def resolve_posted_range(
    posted_from: str | None,
    posted_to: str | None,
    days_back: int | None,
) -> tuple[dt.date, dt.date]:
    """Return (postedFrom, postedTo) as dates. Max span 365 days (SAM rule)."""
    today = dt.datetime.now(dt.UTC).date()
    d_to = parse_mmddyyyy(posted_to) or today
    d_from = parse_mmddyyyy(posted_from)
    if d_from is None:
        back = max(1, min(int(days_back or 7), 365))
        d_from = d_to - dt.timedelta(days=back)
    if d_from > d_to:
        raise ValueError("postedFrom is after postedTo")
    if (d_to - d_from).days > 365:
        raise ValueError("SAM.gov requires postedFrom/postedTo within 1 year; shorten the window")
    return d_from, d_to


def _place(pop: dict | None) -> dict | None:
    if not isinstance(pop, dict):
        return None
    pop = strip_contacts(pop)
    city = pop.get("city") if isinstance(pop.get("city"), dict) else {}
    state = pop.get("state") if isinstance(pop.get("state"), dict) else {}
    country = pop.get("country") if isinstance(pop.get("country"), dict) else {}
    out = {
        "streetAddress": pop.get("streetAddress") or pop.get("streetAddress2"),
        "city": city.get("name") if city else (pop.get("city") if isinstance(pop.get("city"), str) else None),
        "state": state.get("code") or state.get("name"),
        "zip": pop.get("zip"),
        "country": country.get("code") or country.get("name"),
    }
    return {k: v for k, v in out.items() if v} or None


def _award(award: dict | None) -> dict | None:
    if not isinstance(award, dict):
        return None
    award = strip_contacts(award)
    awardee = award.get("awardee") if isinstance(award.get("awardee"), dict) else {}
    awardee = strip_contacts(awardee) if awardee else {}
    loc = awardee.get("location") if isinstance(awardee.get("location"), dict) else None
    amount = award.get("amount")
    try:
        amount_f = float(amount) if amount not in (None, "") else None
    except (TypeError, ValueError):
        amount_f = None
    return {
        "date": award.get("date"),
        "number": award.get("number"),
        "amount": amount_f,
        "amountRaw": str(amount) if amount not in (None, "") else None,
        "awardeeName": awardee.get("name"),
        "awardeeUei": awardee.get("ueiSAM"),
        "awardeeLocation": _place(loc) if loc else None,
    }


def flatten_notice(raw: dict, *, description_excerpt: str | None = None) -> dict[str, Any]:
    """Turn one opportunitiesData[] element into a stable output row (no contacts)."""
    raw = strip_contacts(raw)
    notice_id = raw.get("noticeId") or raw.get("noticeid")
    agency = raw.get("fullParentPathName") or " · ".join(
        x for x in [raw.get("department"), raw.get("subTier") or raw.get("subtier"), raw.get("office")] if x
    ) or None
    ui = raw.get("uiLink")
    if isinstance(ui, str) and ui.lower() in ("null", "none", ""):
        ui = None
    if not ui and notice_id:
        ui = f"https://sam.gov/opp/{notice_id}/view"

    desc = description_excerpt
    if desc is None:
        # description field is usually a URL to noticedesc, not body text
        d = raw.get("description")
        if isinstance(d, str) and d and not d.startswith("http") and d.lower() != "null":
            desc = scrub_text(d)

    naics = raw.get("naicsCode") or raw.get("ncode")
    if naics is not None:
        naics = str(naics)

    award = _award(raw.get("award") if isinstance(raw.get("award"), dict) else None)

    office = raw.get("officeAddress") if isinstance(raw.get("officeAddress"), dict) else None
    office = strip_contacts(office) if office else None

    return {
        "noticeId": notice_id,
        "title": scrub_text(raw.get("title")),
        "solicitationNumber": (raw.get("solicitationNumber") or "").strip() or None,
        "agency": scrub_text(agency),
        "fullParentPathCode": raw.get("fullParentPathCode"),
        "organizationType": raw.get("organizationType"),
        "type": raw.get("type"),
        "baseType": raw.get("baseType"),
        "naicsCode": naics,
        "classificationCode": raw.get("classificationCode"),
        "setAside": raw.get("typeOfSetAsideDescription") or raw.get("setAside"),
        "setAsideCode": raw.get("typeOfSetAside") or raw.get("setAsideCode"),
        "postedDate": raw.get("postedDate"),
        "responseDeadline": raw.get("responseDeadLine") or raw.get("responseDeadline") or raw.get("reponseDeadLine"),
        "archiveDate": raw.get("archiveDate"),
        "archiveType": raw.get("archiveType"),
        "active": raw.get("active"),
        "award": award,
        "awardAmount": award.get("amount") if award else None,
        "awardNumber": award.get("number") if award else None,
        "awardDate": award.get("date") if award else None,
        "awardeeName": award.get("awardeeName") if award else None,
        "placeOfPerformance": _place(raw.get("placeOfPerformance") if isinstance(raw.get("placeOfPerformance"), dict) else None),
        "officeAddress": office,
        "uiLink": ui,
        "additionalInfoLink": raw.get("additionalInfoLink") if raw.get("additionalInfoLink") not in (None, "null") else None,
        "resourceLinks": [u for u in (raw.get("resourceLinks") or []) if isinstance(u, str) and u.startswith("http")][:20] or None,
        "descriptionExcerpt": scrub_text(desc) if desc else None,
        "descriptionUrl": (
            raw.get("description")
            if isinstance(raw.get("description"), str) and raw.get("description", "").startswith("http")
            else None
        ),
    }


async def _request(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None,
    api_key: str | None,
    max_retries: int = 3,
    timeout: float = 30.0,
    backoff_base: float = 0.5,
) -> tuple[int, Any, int, str]:
    """Return (status, json_or_none, attempts, error_text). Retries 429/5xx/timeouts. Never retries missing_key."""
    attempts = 0
    last_status = 0
    last_text = ""
    last_json: Any = None
    q = dict(params or {})
    if api_key:
        q["api_key"] = api_key
    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        try:
            resp = await client.get(
                url,
                params=q,
                timeout=timeout,
                headers={"Accept": "application/json", "User-Agent": UA},
            )
            last_status = resp.status_code
            try:
                last_json = resp.json()
            except Exception:  # noqa: BLE001
                last_json = None
            last_text = redact_secret(resp.text[:800], api_key)
            if resp.status_code == 200:
                return 200, strip_contacts(last_json), attempts, ""
            # 404 without key / invalid key — do not retry
            if resp.status_code in (400, 401, 403, 404):
                return resp.status_code, last_json, attempts, last_text
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= max_retries:
                    break
                ra = resp.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra else backoff_base * (2 ** attempt) + random.uniform(0, 0.25)
                except ValueError:
                    delay = backoff_base * (2 ** attempt)
                await asyncio.sleep(min(delay, 60))
                continue
            return resp.status_code, last_json, attempts, last_text
        except httpx.TimeoutException as e:
            last_status = 0
            last_text = f"timeout: {e}"
            if attempt >= max_retries:
                break
            await asyncio.sleep(backoff_base * (2 ** attempt))
        except httpx.HTTPError as e:
            last_status = 0
            last_text = redact_secret(f"network: {e}", api_key)
            if attempt >= max_retries:
                break
            await asyncio.sleep(backoff_base * (2 ** attempt))
    return last_status or 0, last_json, attempts, last_text


def build_search_params(
    *,
    posted_from: dt.date,
    posted_to: dt.date,
    limit: int,
    offset: int,
    keywords: str | None = None,
    ncode: str | None = None,
    ptype: str | None = None,
    organization_name: str | None = None,
    solnum: str | None = None,
    notice_id: str | None = None,
    state: str | None = None,
    type_of_set_aside: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "postedFrom": format_mmddyyyy(posted_from),
        "postedTo": format_mmddyyyy(posted_to),
        "limit": max(1, min(int(limit), 1000)),
        "offset": max(0, int(offset)),
    }
    if keywords:
        params["title"] = keywords  # official title filter
    if ncode:
        params["ncode"] = str(ncode).strip()
    if ptype:
        # SAM accepts multi ptype; pass comma-joined string (also works as single)
        params["ptype"] = str(ptype).strip()
    if organization_name:
        params["organizationName"] = organization_name.strip()
    if solnum:
        params["solnum"] = solnum.strip()
    if notice_id:
        params["noticeid"] = notice_id.strip()
    if state:
        params["state"] = state.strip().upper()
    if type_of_set_aside:
        params["typeOfSetAside"] = type_of_set_aside.strip()
    if status:
        params["status"] = status.strip().lower()
    return params


async def search_page(
    client: httpx.AsyncClient,
    *,
    api_key: str | None,
    params: dict[str, Any],
    max_retries: int = 3,
    timeout: float = 30.0,
) -> SearchPage:
    status, data, attempts, err = await _request(
        client, SAM_BASE, params=params, api_key=api_key, max_retries=max_retries, timeout=timeout,
    )
    has_key = bool(api_key)
    if status != 200 or not isinstance(data, dict):
        ec = classify_error(status, err, has_key=has_key) if status else (
            "timeout" if "timeout" in (err or "") else "network"
        )
        hint = KEY_HINT if ec in ("missing_key", "api_key_invalid", "unauthorized") else None
        if ec == "rate_limited":
            hint = "SAM.gov rate-limited (HTTP 429) after retries. Reduce maxConcurrency or retry later."
        if ec == "bad_request":
            hint = "SAM.gov rejected parameters (HTTP 400). Check date format MM/dd/yyyy and 1-year window."
        if ec == "not_found" and has_key:
            # With a valid key, 404 can also mean zero results for some error paths;
            # treat as empty page rather than hard fail when body says no data.
            hint = "No opportunities matched (HTTP 404). Widen dates or relax filters."
        return SearchPage(
            ok=False, status=status, attempts=attempts,
            error_class=ec, error_message=(err or ec)[:500], hint=hint,
        )
    opps = [o for o in (data.get("opportunitiesData") or []) if isinstance(o, dict)]
    try:
        total = int(data.get("totalRecords") or 0)
    except (TypeError, ValueError):
        total = len(opps)
    return SearchPage(
        ok=True, total_records=total, opportunities=opps,
        status=200, attempts=attempts,
    )


async def fetch_description(
    client: httpx.AsyncClient,
    notice_id: str,
    *,
    api_key: str | None,
    max_chars: int = 800,
    max_retries: int = 2,
    timeout: float = 30.0,
) -> str | None:
    """Optional GET noticedesc. Returns scrubbed plain text excerpt or None. Failures are silent."""
    if not notice_id or not api_key:
        return None
    status, data, _, _ = await _request(
        client, SAM_DESC_BASE,
        params={"noticeid": notice_id},
        api_key=api_key, max_retries=max_retries, timeout=timeout,
    )
    if status != 200:
        return None
    text: str | None = None
    if isinstance(data, dict):
        for k in ("description", "descriptionText", "content", "body", "text"):
            if isinstance(data.get(k), str) and data[k].strip():
                text = data[k]
                break
        if text is None:
            # sometimes wrapped
            inner = data.get("data")
            if isinstance(inner, dict):
                for k in ("description", "descriptionText", "content"):
                    if isinstance(inner.get(k), str) and inner[k].strip():
                        text = inner[k]
                        break
            elif isinstance(inner, str):
                text = inner
    elif isinstance(data, str):
        text = data
    if not text:
        return None
    text = scrub_text(text.strip())
    if not text:
        return None
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def fail_item(
    *,
    notice_id: str | None = None,
    title: str | None = None,
    error_class: str,
    error_message: str | None = None,
    http_status: int | None = None,
    attempts: int = 0,
    hint: str | None = None,
) -> dict[str, Any]:
    return {
        "noticeId": notice_id,
        "title": title,
        "agency": None,
        "naicsCode": None,
        "type": None,
        "postedDate": None,
        "responseDeadline": None,
        "awardAmount": None,
        "uiLink": None,
        "descriptionExcerpt": None,
        "markdown": None,
        "status": "failed",
        "errorClass": error_class,
        "errorMessage": (error_message or error_class)[:500],
        "httpStatus": http_status,
        "attempts": attempts,
        "hint": hint or (KEY_HINT if error_class in ("missing_key", "api_key_invalid", "unauthorized") else None),
    }
