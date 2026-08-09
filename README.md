# FPS-Level Sale Data Scraper — Goa, March & April 2026

**Target:** https://impds.nic.in/sale/ (IMPDS — Automated ePoS Distribution Dashboard)
**Deliverables:** `get_raw_data.py`, `consolidate_data.py`

---

## 1. Problem statement

Build a scraper that collects **Fair Price Shop (FPS) level sale transaction data** for **Goa** -both districts, for **March 2026** and **April 2026** with proper error handling and logging.

**The approach in short:** the site has no links to follow - it fetches everything through background calls and remembers where you are on the server. So rather than automating the clicks in a browser with Selenium or Playwright, the script makes those same calls directly with a `requests` session: set the month and district, read the shop list the site itself publishes, then pull one page per shop and save it.

That works out to one request per shop instead of the nine a browser fires, with no browser to start or wait on - the full run of 904 shop requests finishes in about 10 minutes it is **efficient and requires much less compute**.

---

## 2. Analysis — working out how the site actually delivers data

### 2.1 First observation: the URL never changes

Clicking down the hierarchy - state, district, shop changed the content on screen but **never changed the address bar**. So the page was fetching data in the background, and there was no URL I could simply copy and iterate over. Whatever the script had to do, it wasn't going to be "loop over a list of links."

### 2.2 DevTools first, then a Playwright wiretap

The obvious next step was the browser's own Network tab (`F12`). It didn't give me a reliable picture of the background calls — I could not consistently get the drill-down requests to surface there. Rather than guess at the cause, I switched to an approach that records requests independently of the browser UI.     

I wrote **`network_analysis.py`**, a small Playwright script that opens a real browser, listens on every request and response, filters out CSS/JS/image noise, and **saves each `/sale/` response body to disk**. Then I drilled the hierarchy manually while it recorded.

That produced 15 captured responses in `capture/`, and made the whole mechanism visible.

### 2.3 What the wiretap showed

Every meaningful click fired a plain **GET**, returned **200**, and put all its detail in the query string:

| Action | Request |
|---|---|
| Page loads | `/sale/` |
| Month summary | `/sale/stateUnautmated?month=3&year=2026` |
| States popup | `/sale/liveStatesAjax` |
| Selected Goa | `/sale/stateByCountryAjax?stateCode=30` |
| Districts popup | `/sale/liveDistrictAjax?stateCode=30` |
| District page | `/sale/districtByCountryAjax?stateCode=585` |
| FPS list | `/sale/fpsByCountryAjax2?stateCode=30` |
| One shop | `/sale/fpsByCountryAjax?stateCode=158500100001` |

Three things stood out immediately:

- **The responses are HTML, not JSON.** The server returns a ready-made `<table>` fragment that the page drops into place. So parsing means HTML, not a neat JSON object.
- **No login, no CSRF token, no captcha** anywhere on this path.
- **The `+` on Coarse Grains fires no request at all.** The six grain rows are already inside the shop response as hidden rows (`display:none`); the `+` only toggles visibility. So all six grains come for free — no extra call, and no need to simulate a click.

### 2.4 The catch: the server remembers what you clicked

I copied a working shop URL and opened it in a fresh Incognito window. Same URL, different result: **"FPS LIST 0 — DATA NOT AVAILABLE."**

So the calls are **not independent**. The server holds state against your session and only serves shop data if you have already "walked" to that district in the same session. The shop ID being right there in the URL is not enough.

### 2.5 Where the month comes from

Looking back at the calls in 2.3, exactly **one** of them carries a month — `stateUnautmated`. Every drill-down call after it (`stateByCountryAjax`, `districtByCountryAjax`, `fpsByCountryAjax2`, `fpsByCountryAjax`, and the two `live*` popups) carries only a `stateCode` and no month at all.

So the month is never passed down the chain. It is set once, by loading:

```
stateUnautmated?month=M&year=2026
```

Changing the month in the dropdown runs `window.location = "stateUnautmated?month=..."`, i.e. a full page load rather than a background call. Once that page is loaded, the month sits in session state and the drill-down after it returns that month's figures.

So the order matters: **set the month first, then walk the drill-down in the same session.** Doing it the other way round gives whatever month the session was already on.

I checked that the month genuinely reaches shop level rather than only changing the top-level summary — same shop pulled for March and then April, and about **160 of ~219 numbers differed**, 30 of them for that single shop.

---

## 3. The decision: session-based `requests`, not a Playwright or a selenium-driven scraper

Both routes work. They are not equally good here.

| | Playwright-driven | `requests` + BeautifulSoup |
|---|---|---|
| Effort to build | Low — replay the clicks | Higher — requires the analysis above |
| Requests per shop | All 9 UI calls | 1 |
| Speed | Slow (browser startup, rendering, waits) | ~0.5 s per shop |
| Fragility | Breaks on any layout/selector change | Breaks only if endpoints change |

The deciding fact is that **the data arrives as server-rendered HTML**. The site does use JavaScript — jQuery drives the entire drill-down — but the JS only *places* HTML the server already built. Nothing needs to be executed client-side to obtain the numbers, so a browser engine adds cost and no capability.

Playwright is the easy implementation, but it is not the right one for a production-grade script that has to iterate ~450 shops across two months. The `requests` approach needs the deeper analysis up front, but once that analysis is done it is far faster and far more stable — and that is what I have implemented.

**Playwright still earned its place**: it was the right tool for *discovery*, and the responses it captured became the ground truth I checked the `requests` output against. I confirmed a plain `requests.Session()` returns **exactly** the same figures as the browser for the same shop.

---

## 4. Narrowing discovery into an actual specification

Knowing the site *can* be scraped this way is not the same as knowing *which calls to fire*. The captured list has nine calls; some are only there because of how the page is built. So I tested each assumption by hand-building request chains and removing one call at a time.

This could not be done with Playwright: a browser always fires all nine calls, so it can never tell you which are unnecessary. Proving a call is optional requires omitting it and seeing whether the server still answers.

### 4.1 Only two calls actually matter

| Chain | Result |
|---|---|
| Full 6-call walk | 240 shops  |
| Drop both `live*` popups | **byte-identical**  |
| Also drop `stateByCountryAjax` | **byte-identical**  |
| Also drop `districtByCountryAjax` | collapses to "DATA NOT AVAILABLE" |

So `liveStatesAjax`, `liveDistrictAjax` and `stateByCountryAjax` are **pure UI** — they fill on-screen popups and hold no state I need. The working sequence is:

```
GET stateUnautmated?month=M&year=2026        →  sets the MONTH
GET districtByCountryAjax?stateCode=585|586  →  sets the DISTRICT
GET fpsByCountryAjax2?stateCode=30           →  the FPS list for that district
GET fpsByCountryAjax?stateCode=<12-digit>    →  one shop  (repeat per shop)
```

### 4.2 The server tracks exactly two things

Against a `PDS_SESSION_ID` cookie: the **month** and the **current district**. Nothing else.

A useful consequence: **`fpsByCountryAjax2`'s `stateCode` parameter is ignored entirely.** I called it with `30`, `585`, `586`, `99` and `xyz` — all five returned the same 240 shops. District scoping comes purely from the session, so there is no hidden `district=` parameter to hunt for.

### 4.3 Getting the shop list right

Shop codes skip numbers — `…0001`, `…0002`, then `…0005` — so counting upwards would ask for shops that don't exist. Instead the script reads the list straight off the FPS panel, so every code it uses is one the site itself published.

The list already comes split by district: **240 shops for North Goa** (all codes start `1585`) and **212 for South Goa** (all start `1586`), with no overlap. The district code sits inside the shop code, so the codes confirm their own district.

To be sure nothing was missed, I compared my count against the site's own "Fair Price Shops" counter — **240 and 212, an exact match**.

This list is saved as `_manifest.json` inside each month/district folder, for example `data/raw/2026-03/north_goa/_manifest.json`, so the files written can always be checked against what the site actually listed.

---

## 5. Error handling, retries and integrity checks

I did not want to assume what failures look like, so I sent deliberately broken requests and watched what came back.

### 5.1 Two very different kinds of "200"

A failed shop request still returns **HTTP 200**. There is no exception to catch and — importantly — no `DATA NOT AVAILABLE` text either. But two different things can be wrong, and they are not equally serious:

| Case | What it means | Damage | Response |
|---|---|---|---|
| **Panel missing** | the shop returned nothing | a **hole** — data is incomplete, but the log shows it | log and skip, never retry |
| **Panel present, wrong FPS Id** | the server's position disagrees with my request | **corruption** — another shop's numbers written into this shop's file, and the dataset *looks* complete | rebuild the session, ask once more, and **abort** if it happens again |

This is why the integrity check is "**the FPS Id shown in the panel must equal the code I asked for**", not merely "did I get a page back". A missing panel costs completeness; a mismatched panel costs correctness, which is worse, because nothing downstream would ever reveal it.

### 5.2 Not all 500s are worth retrying

My first plan was the usual one: on any 500, wait 2s, 4s, 8s and try again. But that assumes the server is busy, and a 500 can just as easily mean the server choked on the input — in which case waiting changes nothing.

To tell the two apart I sent a handful of bad values in `stateCode` (letters, an empty value, wrong digit counts, no parameter at all) and sent **each one three times**. Every one of them failed exactly the same way all three times, which told me the problem was the input, not the server being busy — waiting and trying again would never have helped.

So the rule keys on that instead of on the status code:

> On a 5xx, **retry once**. If the second response is identical, waiting won't help — skip it. If it differs, the server really is unstable — then use 2s / 4s / 8s.

A broken input now costs about half a second instead of fourteen, and real instability still gets three full attempts. I also checked that a 500 doesn't damage the session: after four in a row, the next valid request answered normally, so there's no need to rebuild after a server error.

### 5.3 The reference check — catching the failure nothing else can

There is one failure the FPS Id check cannot catch. **The shop response contains no month stamp anywhere.** I searched for one; there is none. So if the session's month ever drifted, the response would be well-formed, the FPS Id would match, and the numbers would simply be from the wrong month. Silently.

So I keep a **reference shop**. At the start of each session I read one shop and remember its numbers. Every 50 shops I read that same shop again and compare. It should never change mid-run, so if it does, the session has moved and the run stops.

One thing to note: a failed reference check only proves the session moved *somewhere since the last check*, not exactly where. So the gap between checks decides how much data is in doubt — at 50, a failure at shop 200 puts shops 150–200 in question, not just shop 200.

---

## 6. Architecture

The month can only drift if something changes it. So rather than one long-lived session switching months and districts, the script uses **four isolated sessions** — each sets its position once and never touches it again.

```
                    ┌─────────────────────────────┐
                    │      4 Isolated Sessions    │
                    └──────────────┬──────────────┘
         ┌────────────────┬────────┴───────┬────────────────┐
         ▼                ▼                ▼                ▼
   [ Session 1 ]    [ Session 2 ]    [ Session 3 ]    [ Session 4 ]
    March 2026       March 2026       April 2026       April 2026
   North Goa 585    South Goa 586    North Goa 585    South Goa 586
```

Each session then runs the same sequence:

```
  build_session(month, district)
      GET stateUnautmated?month=M&year=2026      ← set month   (once)
      GET districtByCountryAjax?stateCode=D      ← set district (once)
             │
             ▼
  fetch_manifest()          GET fpsByCountryAjax2   → 240 or 212 shops
             │
             ▼
  reference reading         read one shop, remember its numbers
             │
             ▼
  ┌──────────── for each shop ────────────┐
  │  file already exists?  → skip (resume) │
  │  GET fpsByCountryAjax?stateCode=<id>   │
  │        ├─ 5xx        → retry once, compare, skip or back off
  │        ├─ no panel   → log + skip
  │        ├─ mismatch   → rebuild session → still wrong? ABORT
  │        └─ ok         → parse + write JSON
  │  every 50 shops → re-read reference shop│
  └────────────────────────────────────────┘
```

I verified the four sessions really are independent — two sessions from the same machine, given deliberately conflicting positions and interleaved, each kept its own district and month with no cross-talk. State is tied to the cookie, not the machine.

They are run **sequentially by choice**. They could safely run in parallel, but this is a government server, and 904 requests already finish in about ten minutes. The speed-up isn't worth the extra load.

---

## 7. What the script produces

```
data/
├── raw/
│   ├── 2026-03/
│   │   ├── north_goa/   158500100002_m_s_p_s_karekar_26_fps.json  ... (240)
│   │   │                _manifest.json, _run_summary.json
│   │   └── south_goa/   ... (212)
│   └── 2026-04/         ... (same structure)
└── processed/           fps_sales_goa_2026.csv  (written by consolidate_data.py, S8)
```

Each shop file holds the identity fields (`year`, `month`, `state`, `district`, `fps_id`, `fps_name`), the four summary cards, and all three tables with Coarse Grains expanded into its six sub-commodities. Every cell is stored as `{"raw": "1,885", "value": 1885}` — the original text alongside the parsed number, so any figure can be audited without re-scraping.

Other behaviour required by the brief:

- **Resumable** — a shop whose file already exists is never re-fetched, so an interrupted run continues where it stopped.
- **Logging** — progress goes to console and `scrape_run.log`, with per-district counts and every failure recorded by shop ID.
- **Separation** — this script only collects raw data. Cleaning and consolidation are a separate pass, so a parsing bug can never damage collected data.

**Total scope:** 240 + 212 shops × 2 months = **904 shop requests**, ~6–10 minutes.

---

## 8. Consolidation — `consolidate_data.py`

The second script turns those 904 JSON files into one CSV, **one row per shop per month**.

### 8.1 Flattening

Each shop file has four summary cards and three tables. Flattening walks them in a fixed order and produces **76 columns**:

| Group | Columns | Example |
|---|---|---|
| Identity | 8 | `fps_id`, `fps_name`, `district`, `month` |
| Summary cards | 7 | `total_etransactions`, `aadhaar_authenticated_pct` |
| Transactions | 8 | `phh_regular_txn`, `aay_inter_state_txn` |
| Ration cards | 8 | `phh_total_ration_card` |
| Quantity | 44 | `rice_regular_kg`, `barley_intra_state_kg` |
| Flags | 1 | `quality_flags` |

Names follow one pattern throughout — `{who}_{which transaction}_{what}` — so a column can be read without a lookup table.

The summary cards are seven columns rather than four because three of them show a percentage under the count on the page as well — `Aadhaar Authenticated` reads `279` and `100%`. Both are taken as shown. The Total card has no percentage, so it has no `_pct` column.

### 8.2 Cleaning

Most of the cleaning already happened at scrape time. Every cell is stored as `{"raw": "3,890", "value": 3890}`, so the consolidation reads `value` and only re-parses `raw` if `value` came back `None`.

The rule that matters is **blank stays blank**. A missing figure is written as an empty cell, never as `0` — because "this shop sold nothing" and "this number didn't load" are different facts, and turning one into the other is invisible once it's in a CSV. The same applies to `aadhaar_authenticated_pct`: a shop with zero transactions gets a blank, not `0%`.

### 8.3 Flags instead of dropped rows

Nothing is ever silently dropped or corrected. Anything odd is recorded in a `quality_flags` column and the row is kept, so the decision about what to exclude stays with whoever does the analysis.

| Flag | What it means |
|---|---|
| `missing_table:<name>` | one of the three tables didn't load |
| `no_transactions` | a real shop with zero sales that month |
| `cards_dont_sum` | the four summary cards don't add to the headline total |
| `commodity_total_mismatch` | the site's Total row ≠ Wheat + Rice + Coarse Grains |

Every commodity row on the page gets its own columns, exactly as the site shows them. Nothing is combined or recalculated.

### 8.4 Completeness

Before finishing, the script compares the rows written against the totals in every `_manifest.json`. If they disagree — or anything was flagged or unreadable — it exits with code 1, so a short CSV can't pass as a finished one.

**Output:** `data/processed/fps_sales_goa_2026.csv`

---

## 9. Limitations

- **The 2s / 4s / 8s waits are a sensible guess, not a measurement.** Every 500 I managed to trigger was caused by bad input, and the server was never actually busy while I was testing. So I have never seen the case those waits are for. I kept them because waiting when you didn't need to costs a few seconds, while not waiting could kill a whole run.
- **Two of the error checks have never fired.** The shop ID always matched what I asked for, and the reference shop never changed. Both are there for problems I have not actually seen happen.
- **You have to edit the script to change what it scrapes.** There is no command line option. Three variables at the top — `MONTHS_TO_SCRAPE`, `DISTRICTS_TO_SCRAPE`, `SHOP_LIMIT` — control it, and as written they already cover the full assignment. Fine here, since the scope never changes, but it does mean editing the file instead of passing an argument.
- **Only March and April 2026, Goa.** The month dropdown and the district codes are fixed in the script. Another state would need its own state code and a check that it behaves the same way.

---

## 10. Assumptions

- **The figures don't change while a run is going.** A run takes about ten minutes. If the site updated a shop's numbers halfway through, the first half and second half of the data would be from different moments and I wouldn't know.
- **The coarse grain zeros are real.** All six came back zero for every shop. I have taken that as genuinely zero rather than something failing to load, because the totals add up correctly without them.

---

## 11. How to run it

**Requirements** — Python 3.8 or newer, and two packages:

```bash
pip install -r requirements.txt
```

Only `requests` and `beautifulsoup4`. No pandas — the consolidation writes one row per file with nothing to group or join, so `csv` from the standard library does it.

**Step 1 — collect the raw data**

```bash
python get_raw_data.py
```

Fetches all 452 shops for both months (~10 minutes) and writes one JSON per shop under `data/raw/`, plus `scrape_run.log`. Safe to stop with Ctrl+C and run again — it skips shops it already has.

**Step 2 — build the CSV**

```bash
python consolidate_data.py
```

Reads everything under `data/raw/` and writes `data/processed/fps_sales_goa_2026.csv`. Prints nothing; exits 0 if the data is complete and clean, 1 if rows are missing or flagged.

Run step 1 first — step 2 has nothing to read otherwise. On Windows, use `py` in place of `python` if that is where your packages are installed.

---

## 12. Repository layout

```
.
├── get_raw_data.py        collects the raw data     (step 1)
├── consolidate_data.py    builds the csv            (step 2)
├── network_analysis.py    the Playwright wiretap used for discovery (section 2.2)
├── requirements.txt       the two packages needed
├── README.md              this file
├── capture/               the 15 responses the wiretap recorded
└── data/
    ├── raw/               one json per shop, per month, per district
    └── processed/         fps_sales_goa_2026.csv
```

`data/raw/` and `capture/` are kept in the repository on purpose. The raw JSON is the evidence behind every number in the CSV, and `capture/` is the recorded traffic behind the analysis in section 2 — so both the data and the reasoning can be checked without re-running anything.

`network_analysis.py` needs Playwright, which the two scrapers do not. It is included because section 2 refers to it, not because it is needed to produce the data.

