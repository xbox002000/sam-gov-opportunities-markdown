# SAM.gov Opportunities → Markdown Notice Cards

Search **U.S. federal contract opportunities** on [SAM.gov](https://sam.gov/) via the **official Opportunities API v2** and get clean **JSON rows** plus **Markdown notice cards** you can read, paste into a CRM, or feed to an LLM / RAG pipeline.

- **Official HTTP only.** `https://api.sam.gov/opportunities/v2/search` (and the related official noticedesc endpoint). **No HTML scrape** of sam.gov pages.
- **Bring your own key (BYOK).** Free public API key from SAM.gov Account Details. Without a key the API returns **HTTP 404**; those rows are free.
- **Light:** 256 MB default, offset/limit pagination, stops at `maxNotices`.
- **Failures are free.** Missing/invalid key, empty search, rate limits, timeouts and server errors are logged as free rows.
- **No contacts / leads.** `pointOfContact` blocks are dropped. Emails and phones in free text are replaced with `[email removed]` / `[phone removed]`.

Part of the same **official procurement → Markdown** product line as **UK Tenders — Find a Tender + Contracts Finder OCDS** (UK), not a redo of that Actor.

## Get a free SAM.gov public API key

1. Create / sign in at [https://sam.gov/](https://sam.gov/)
2. Open **Account Details**
3. Generate / view your **Public API Key** (also documented on [open.gsa.gov — Get Opportunities Public API](https://open.gsa.gov/api/get-opportunities-public-api/))
4. Paste it into the Actor input field `samApiKey` (stored encrypted; never logged)

The Opportunities API public key is **free**. This Actor does **not** buy, store, or share a publisher-owned SAM key — you bring your own.

## What you get

**Dataset**: one row per notice:

| Field | Example |
|---|---|
| `noticeId`, `solicitationNumber` | `5b345bbb7127b91a3ad577b203fc6f68`, `47PF0018R0023` |
| `title`, `agency` | "Historic Office Renovation", "GENERAL SERVICES ADMINISTRATION.…" |
| `type`, `baseType`, `naicsCode`, `setAside` | Solicitation, Combined Synopsis/Solicitation, `236220` |
| `postedDate`, `responseDeadline`, `active` | ISO / SAM date strings |
| `awardAmount`, `awardNumber`, `awardDate`, `awardeeName` | when award data is present |
| `placeOfPerformance`, `uiLink` | structured PoP + https://sam.gov/opp/…/view |
| `descriptionExcerpt`, `markdown` | scrubbed excerpt + full notice card |
| `status`, `errorClass`, `hint` | `ok` / `failed` |

**Key-value store**
- `REPORT.md` — summary table + one Markdown card per notice + "Not charged" section
- `REPORT.json`, `SUMMARY`, `OUTPUT` — counts, filters, charged events, duration, peak memory

### Example card

```markdown
### Historic Office Renovation

- **Agency:** GENERAL SERVICES ADMINISTRATION.PUBLIC BUILDINGS SERVICE.PBS R5
- **IDs:** notice `5b345bbb…` · sol `47PF0018R0023` · [view on SAM.gov](https://sam.gov/opp/…/view)
- **Type:** Award Notice (base: Combined Synopsis/Solicitation)
- **NAICS:** `236220`
- **Dates:** posted 2018-05-04
- **Award / value:** $800,620 · 47PF0018C0066 · D.G. Beyer, Inc.

> Renovation of historic office space …
```

## Input (high level)

| Field | Default | Notes |
|---|---|---|
| `samApiKey` | — | **Required for live data** (isSecret). No key → 404 / `missing_key` free |
| `keywords` | — | Official `title` filter |
| `postedFrom` / `postedTo` | daysBack=7 | **MM/dd/yyyy**, max 1 year apart (SAM rule) |
| `daysBack` | 7 | Used when `postedFrom` is empty |
| `ncode` | — | NAICS (≤6 digits) |
| `ptype` | — | e.g. `o,k` (solicitation / combined) |
| `organizationName`, `solnum`, `state`, `typeOfSetAside`, `status` | — | Official filters |
| `noticeIds` | — | Optional direct noticeId list (still needs date window) |
| `maxNotices` | 50 | Cap successful cards |
| `pageSize` | 100 | Official `limit` (max 1000); paging via `offset` |
| `maxConcurrency` | 2 | Description / noticeId parallelism |
| `maxRetries` | 3 | 429 / 5xx / timeout only |
| `fetchDescriptions` | true | Optional official noticedesc excerpt |

## Pricing (pay-per-event)

| Event | Price |
|---|---|
| Actor start | $0.001 |
| `opportunity-notice` (primary) | **$0.003** per successful notice card |

Failures (missing key, invalid key, not found, empty search, rate limit, 5xx) are **free**.

## Diff vs Store scrapers

| | This Actor | Typical Store scrapers |
|---|---|---|
| Source | Official Opportunities API v2 | HTML / browser scrapes |
| Memory | **256 MB** | often 1–4 GB |
| Contacts | **Never** (POC dropped) | often marketed as Contacts / Leads |
| Auth | Buyer public API key | "No key" scrapes that break |
| Output | Markdown notice cards + JSON | raw scrape rows |

## Legal / attribution

Data comes from the U.S. General Services Administration SAM.gov public Opportunities API. You are responsible for complying with SAM.gov / GSA terms and your own daily API quota. This Actor does not scrape HTML and does not harvest contact lists.

## Source code

Open source (AGPL-3.0): https://github.com/xbox002000/sam-gov-opportunities-markdown
