import json     # one json file per shop, thats also the output format
import logging  # progress on screen and a log file to read after a run
import os       # paths, folders, and checking if a shop is already done
import re       # pulling the 12 digit shop codes out of the html
import sys      # console logging and the exit code
import time     # backoff waits and how long the run took
from datetime import datetime, timezone  # timestamp saved inside each file
import requests              # keeps the cookie, which is what holds month + district
from bs4 import BeautifulSoup  # the responses are html tables, not json

# -- CONFIG --

BASE_URL = "https://impds.nic.in/sale/"
YEAR = 2026  # specific year
MONTH_NAMES = {3: "March", 4: "April"}  # months (march and april)
STATE_NAME, STATE_CODE = "GOA", "30"
DISTRICTS = {"585": "NORTH GOA", "586": "SOUTH GOA"}  # zones in goa

# what this run covers. edit these to scrape less, eg MONTHS_TO_SCRAPE = [3] for
# march only, or SHOP_LIMIT = 2 for a quick test that doesnt take ten minutes
MONTHS_TO_SCRAPE = [3, 4]
DISTRICTS_TO_SCRAPE = ["585", "586"]
SHOP_LIMIT = None  # None means every shop in the district

# same headers the page's own jquery sends, no login or token needed anywhere
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE_URL,
}

REQUEST_TIMEOUT = 60  # calls take ~0.5s, this only trips on a hang
TRANSIENT_BACKOFF_SECONDS = (2, 4, 8)  # only for 5xx that CHANGE between tries 
REFERENCE_CHECK_INTERVAL = 50  # how often we re-read the reference shop, also how much data
                              # a failed check puts in doubt 
PROGRESS_LOG_INTERVAL = 25  # just for logging
MAX_SLUG_LENGTH = 80  # keeps windows paths under the 260 char limit

FPS_CODE_PATTERN = re.compile(r"^\d{12}$")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
PROCESSED_DIR = os.path.join(PROJECT_ROOT, "data", "processed")
LOG_PATH = os.path.join(PROJECT_ROOT, "scrape_run.log")

# we find tables by aria-label not by position, so re-ordering the page cant
# silently swap two tables that look the same
TABLE_LABELS = {
    "number_of_transaction": "Number of Transaction",
    "number_of_transacted_ration_card": "Number of Transacted Ration Card",
    "distributed_quantity_kg": "Distributed Quantity(In Kg)",
}

# copied exactly from the html, a small typo here gives None instead of an error
SUMMARY_CARD_LABELS = [
    "Total e-Transaction",
    "Aadhaar Authenticated",
    "Other Mode Authenticated",
    "Non-Authenticated",
]

logger = logging.getLogger("impds")

class SessionDesyncError(RuntimeError):
    """Server is serving a different shop/month than we asked and a rebuild didnt fix it"""

# -- SMALL HELPERS --

def slugify(name):
    """Makes an FPS name safe to use in a filename, returns '' if nothing usable is left"""
    # names like "M/S P S KAREKAR-26 FPS" have a slash in them which would
    # quietly create a folder instead of a file
    lowered = re.sub(r"[^a-z0-9]+", "_", name.lower())
    return re.sub(r"_+", "_", lowered).strip("_")[:MAX_SLUG_LENGTH]

def to_number(text):
    """Turns '3,890' into 3890 and '5425.0' into 5425.0, returns None if not a number"""
    if text is None:
        return None
    cleaned = text.replace(",", "").replace("\xa0", "").strip()
    if not cleaned or cleaned in {"-", "--", "NA", "N/A"}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return int(value) if value.is_integer() and "." not in cleaned else value

def write_json(path, payload):
    """Writes the dict out as readable utf-8 json"""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

def configure_logging(log_path):
    """Logs to the console for live progress and to a file so a failed run can be checked later"""
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%H:%M:%S")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.setLevel(logging.INFO)
    logger.handlers = [console_handler, file_handler]

# -- SESSION AND SHOP LIST --

def build_session(month, district_code):
    """Opens a session and sets its month + district once, thats all the server remembers"""
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    # month comes from session state, set by this page load. the drill-down
    # calls carry no month at all, so this has to come first 
    session.get(BASE_URL + "stateUnautmated",
                params={"month": month, "year": YEAR}, timeout=REQUEST_TIMEOUT)
    # only these 2 calls actually matter. liveStatesAjax, liveDistrictAjax and
    # stateByCountryAjax are just popups, dropping them gives the same bytes
    session.get(BASE_URL + "districtByCountryAjax",
                params={"stateCode": district_code}, timeout=REQUEST_TIMEOUT)
    return session

def describe_session_cookie(session):
    """Short cookie id just for the log, returns 'none' if no cookie yet"""
    # site sets more than one PDS_SESSION_ID so requests' cookies.get() blows up,
    # reading the jar directly instead
    values = [cookie.value for cookie in session.cookies
              if cookie.name == "PDS_SESSION_ID"]
    if not values:
        return "none"
    extra = "" if len(values) == 1 else f" (+{len(values) - 1} more)"
    return f"{values[0][:12]}{extra}"

def fetch_manifest(session):
    """Gets [(fps_code, fps_name)] for whichever district the session is sitting on"""
    # shop codes skip numbers (...0001, 0002, 0005) so we read the list off the
    # site instead of counting up, that way a bad code can never enter the loop 
    # note: stateCode here is ignored by the server, it answers from session
    # state - we only send it because the page does 
    response = session.get(BASE_URL + "fpsByCountryAjax2",
                           params={"stateCode": STATE_CODE}, timeout=REQUEST_TIMEOUT)
    soup = BeautifulSoup(response.text, "html.parser")
    shops = {}
    for anchor in soup.find_all("a", onclick=True):
        # each shop in the left panel looks like onclick="stateData('158500100002')"
        match = re.search(r"stateData\('(\d{12})'\)", anchor["onclick"])
        if not match:
            continue
        label = anchor.get_text(" ", strip=True).replace("\xa0", " ")
        name = label.split(":", 1)[1].strip() if ":" in label else ""
        shops[match.group(1)] = name
    return sorted(shops.items())

# -- PARSING --

def dashboard_block_value(soup, label):
    """Reads the value sitting next to a label, returns None if that label isnt there"""
    # summary cards and the FPS Id block are the same shape: a label div, then
    # an "info" div holding the number
    for label_div in soup.find_all("div", class_=re.compile(r"\bstatus1?\b")):
        text = label_div.get_text(" ", strip=True).rstrip(":").strip()
        if text.lower() != label.lower():
            continue
        info_div = label_div.find_next("div", class_=re.compile(r"\binfo\b"))
        if info_div:
            return info_div.get_text(" ", strip=True).replace("\xa0", " ").strip()
    return None


def dashboard_block_percent(soup, label):
    """Reads the % printed under a summary card, None for a card that doesnt show one"""
    # each card is one metro-nav-block1 div, so the search stays inside that card.
    # find_next would run on into the next card and pick up its percentage instead
    for label_div in soup.find_all("div", class_=re.compile(r"\bstatus1?\b")):
        text = label_div.get_text(" ", strip=True).rstrip(":").strip()
        if text.lower() != label.lower():
            continue
        card = label_div.find_parent("div", class_="metro-nav-block1")
        percent = card.find("span", class_="perce_val") if card else None
        if percent:
            return percent.get_text(" ", strip=True).replace("\xa0", " ").strip()
    return None

def parse_panel_fps_id(soup):
    """Which shop the right panel is actually showing, None if theres no panel at all"""
    # this one check is what the whole run depends on (S5.1). kept strict on
    # purpose - no regex fallback over the full page, because that would match a
    # code from the left hand list and call a broken response a success
    value = dashboard_block_value(soup, "FPS Id")
    if not value:
        return None
    match = re.search(r"\d{12}", value)
    return match.group(0) if match else None

def parse_table(soup, aria_label):
    """Pulls one table into {columns, rows}, returns None if that table isnt in the html"""
    table = soup.find(
        "table",
        attrs={"aria-label": lambda value: value is not None and value.strip() == aria_label},
    )
    if table is None:
        return None

    headers = [cell.get_text(" ", strip=True) for cell in table.find_all("th")]
    rows = {}
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        row_label = cells[0].get_text(" ", strip=True)
        if not row_label:
            continue
        values = [cell.get_text(" ", strip=True).replace("\xa0", " ") for cell in cells[1:]]
        # keeping raw text next to the parsed number so any figure can be
        # checked later without scraping again
        rows[row_label] = {
            column: {"raw": value, "value": to_number(value)}
            for column, value in zip(headers[1:], values)
        }
    return {"columns": headers, "rows": rows}

def parse_shop_panel(html, metadata):
    """Builds the final record for one shop, a table it cant find is stored as None"""
    # the six coarse grains are already in this same response as hidden rows and
    # the "+" only shows/hides them, so no extra request is needed.
    # they are all zero across goa - thats the real data, not a parsing miss
    soup = BeautifulSoup(html, "html.parser")
    record = dict(metadata)
    record["scraped_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    summary_cards = {}
    for label in SUMMARY_CARD_LABELS:
        raw_value = dashboard_block_value(soup, label)
        # the three authentication cards also print a % under the count, the
        # total doesnt, so percent stays None for that one
        raw_percent = dashboard_block_percent(soup, label)
        summary_cards[label] = {
            "raw": raw_value,
            "value": to_number(raw_value),
            "percent_raw": raw_percent,
            "percent": to_number(raw_percent.rstrip("%")) if raw_percent else None,
        }
    record["summary_cards"] = summary_cards

    record["tables"] = {key: parse_table(soup, label) for key, label in TABLE_LABELS.items()}
    return record

# -- FETCHING ONE SHOP --

def fetch_shop_panel(session, fps_code):
    """Fetches one shop, returns (outcome, html) where outcome is ok / no_panel / mismatch / server_error / unreachable"""
    previous_signature = None
    for attempt in range(len(TRANSIENT_BACKOFF_SECONDS) + 1):
        try:
            response = session.get(BASE_URL + "fpsByCountryAjax",
                                   params={"stateCode": fps_code}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as error:
            logger.warning("    %s transport failure on attempt %d: %s",
                           fps_code, attempt + 1, type(error).__name__)
            if attempt == len(TRANSIENT_BACKOFF_SECONDS):
                return "unreachable", None
            time.sleep(TRANSIENT_BACKOFF_SECONDS[attempt])
            continue

        if response.status_code >= 500:
            # we retry based on the CAUSE not the status code (S5.2). every 500
            # we could trigger came back byte identical, meaning it will never
            # succeed, so waiting 2+4+8s on it just wastes 14 seconds
            signature = (response.status_code, len(response.text))
            if signature == previous_signature:
                logger.warning("    %s HTTP %d reproduced exactly - deterministic, skipping",
                               fps_code, response.status_code)
                return "server_error", None
            previous_signature = signature
            if attempt == len(TRANSIENT_BACKOFF_SECONDS):
                return "server_error", None
            # first repeat is instant, its there to classify the error not to
            # wait out load. only an error that CHANGED gets a real delay
            time.sleep(TRANSIENT_BACKOFF_SECONDS[attempt] if attempt else 0)
            continue

        # a failed shop still comes back as 200, so this split below is the
        # actual error handling (S5.1)
        soup = BeautifulSoup(response.text, "html.parser")
        displayed_code = parse_panel_fps_id(soup)
        if displayed_code is None:
            return "no_panel", response.text  # a hole, we just lose this shop
        if displayed_code != fps_code:
            return "mismatch", response.text  # worse, wrong shops numbers
        return "ok", response.text

    return "server_error", None

def read_reference_shop(session, fps_code):
    """Reads a shop we already know the answer for, to prove the session hasnt moved. None if unreadable"""
    # the response has no month written in it anywhere, so the FPS Id check
    # cannot catch month drift - re-reading a known shop is the only way (S5.3). we grab the
    # baseline at session start so it works for any month/district
    outcome, html = fetch_shop_panel(session, fps_code)
    if outcome != "ok":
        return None
    table = parse_table(BeautifulSoup(html, "html.parser"),
                        TABLE_LABELS["distributed_quantity_kg"])
    if not table:
        return None
    return json.dumps(table["rows"].get("Rice"), sort_keys=True)

# -- MAIN LOOP --

def scrape_district_month(month, district_code, limit=None):
    """Scrapes one month x district inside its own session and returns a summary dict"""
    # one session per combination, position set once and never changed (S6).
    # a single long session would also work but every change is a chance to
    # drift, and drift doesnt show up anywhere in the response
    district_name = DISTRICTS[district_code]
    output_dir = os.path.join(RAW_DIR, f"{YEAR}-{month:02d}", slugify(district_name))
    os.makedirs(output_dir, exist_ok=True)

    logger.info("=" * 70)
    logger.info("SESSION  %s %d  |  %s (%s)",
                MONTH_NAMES[month], YEAR, district_name, district_code)
    logger.info("=" * 70)

    # 1 open the session and point it at this month + district
    session = build_session(month, district_code)

    # 2 get the shop list for that district
    manifest = fetch_manifest(session)
    logger.info("  manifest: %d shops (cookie %s)", len(manifest),
                describe_session_cookie(session))
    # always save the FULL list the site published, this is the audit record for
    # the district - SHOP_LIMIT only trims what we fetch, it must not trim this
    write_json(os.path.join(output_dir, "_manifest.json"),
               [{"fps_id": code, "fps_name": name} for code, name in manifest])
    if limit:
        manifest = manifest[:limit]

    # 3 remember one shops numbers so we can spot drift later
    reference_code = manifest[0][0] if manifest else None
    reference_reading = read_reference_shop(session, reference_code) if reference_code else None
    if reference_reading:
        logger.info("  reference shop %s read", reference_code)
    else:
        logger.warning("  reference shop unreadable - month drift undetectable this run")

    counts = {"ok": 0, "skipped_existing": 0, "no_panel": 0,
              "server_error": 0, "unreachable": 0, "session_rebuilds": 0}
    failures = []

    # 4 go through every shop in the list
    for position, (fps_code, fps_name) in enumerate(manifest, start=1):
        # already done in an earlier run, skip it so the run can resume
        output_path = os.path.join(
            output_dir, f"{fps_code}_{slugify(fps_name) or 'unknown'}.json")
        if os.path.exists(output_path):
            counts["skipped_existing"] += 1
            continue

        # costs nothing and cant fire today (all 452 codes are clean), its here
        # in case the list parsing ever breaks in future
        if not FPS_CODE_PATTERN.match(fps_code):
            logger.error("    %s malformed shop code - skipping", fps_code)
            failures.append({"fps_id": fps_code, "reason": "malformed_code"})
            continue

        outcome, html = fetch_shop_panel(session, fps_code)

        if outcome == "mismatch":
            # this one is a correctness problem not just a missing shop, so
            # rebuild and ask again. if its still wrong the session isnt the
            # issue and carrying on would fill the folder with wrong numbers
            logger.error("    %s panel showed a different shop - rebuilding session", fps_code)
            counts["session_rebuilds"] += 1
            session = build_session(month, district_code)
            outcome, html = fetch_shop_panel(session, fps_code)
            if outcome == "mismatch":
                raise SessionDesyncError(
                    f"{fps_code}: panel still showed a different shop after a rebuild "
                    f"- aborting rather than writing corrupt data")

        if outcome != "ok":
            logger.warning("    [%d/%d] %s -> %s", position, len(manifest), fps_code, outcome)
            counts[outcome] = counts.get(outcome, 0) + 1
            failures.append({"fps_id": fps_code, "fps_name": fps_name, "reason": outcome})
            continue

        # 5 parse it and write one json file for this shop
        write_json(output_path, parse_shop_panel(html, {
            "year": YEAR,
            "month": month,
            "month_name": MONTH_NAMES[month],
            "state": STATE_NAME,
            "state_code": STATE_CODE,
            "district": district_name,
            "district_code": district_code,
            "fps_id": fps_code,
            "fps_name": fps_name,
            "source_url": f"{BASE_URL}fpsByCountryAjax?stateCode={fps_code}",
        }))
        counts["ok"] += 1

        if position % PROGRESS_LOG_INTERVAL == 0 or position == len(manifest):
            logger.info("    [%d/%d] %s - written %d, resumed %d, failed %d",
                        position, len(manifest), district_name,
                        counts["ok"], counts["skipped_existing"], len(failures))

        # 6 every 50 shops re-read the reference shop and check it hasnt changed
        if reference_reading and position % REFERENCE_CHECK_INTERVAL == 0:
            if read_reference_shop(session, reference_code) != reference_reading:
                raise SessionDesyncError(
                    f"reference shop changed at shop {position} - every shop since the previous "
                    f"check (up to {REFERENCE_CHECK_INTERVAL}) is suspect and must be re-scraped")
            logger.info("    reference shop still matches at shop %d", position)

    summary = {
        "month": month,
        "month_name": MONTH_NAMES[month],
        "district": district_name,
        "district_code": district_code,
        "manifest_size": len(manifest),
        **counts,
        "failures": failures,
    }
    write_json(os.path.join(output_dir, "_run_summary.json"), summary)
    logger.info("  finished %s %s - written %d, resumed %d, failed %d, rebuilds %d",
                MONTH_NAMES[month], district_name, counts["ok"],
                counts["skipped_existing"], len(failures), counts["session_rebuilds"])
    return summary

# -- MAIN --

def main():
    """Runs every month x district combination. Exit code 0 clean, 1 had failures, 2 desync, 130 interrupted"""
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    configure_logging(LOG_PATH)

    started_at = time.time()
    logger.info("IMPDS scrape | months=%s districts=%s%s",
                MONTHS_TO_SCRAPE, DISTRICTS_TO_SCRAPE,
                f" limit={SHOP_LIMIT}" if SHOP_LIMIT else "")

    summaries = []
    try:
        # done one at a time on purpose. the 4 sessions are independent so they
        # could run together, but this is a government site and 904 requests
        # already finish in about ten minutes (S6)
        for month in MONTHS_TO_SCRAPE:
            for district_code in DISTRICTS_TO_SCRAPE:
                summaries.append(
                    scrape_district_month(month, district_code, SHOP_LIMIT))
    except SessionDesyncError as error:
        logger.error("ABORTED - %s", error)
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted - rerun to resume from where this stopped.")
        return 130

    written = sum(summary["ok"] for summary in summaries)
    resumed = sum(summary["skipped_existing"] for summary in summaries)
    failed = sum(len(summary["failures"]) for summary in summaries)
    logger.info("=" * 70)
    logger.info("COMPLETE in %.1f min - written %d, resumed %d, failed %d",
                (time.time() - started_at) / 60, written, resumed, failed)
    logger.info("Output written to %s", RAW_DIR)
    return 1 if failed else 0

if __name__ == "__main__":
    # sys.exit sends main's return value out as the exit code, so a failed or
    # aborted run doesnt look like success to whatever runs next
    sys.exit(main())
