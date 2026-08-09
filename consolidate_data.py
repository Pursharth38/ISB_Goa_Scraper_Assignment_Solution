import csv     # the output is one flat csv, one row per shop per month
import json    # the raw files written by get_raw_data.py
import os      # walking data/raw and building output paths
import re      # tidying up label text before matching it
import sys     # the exit code

# -- CONFIG --

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
PROCESSED_DIR = os.path.join(PROJECT_ROOT, "data", "processed")
OUTPUT_CSV = os.path.join(PROCESSED_DIR, "fps_sales_goa_2026.csv")

# the scraper writes these two alongside the shop files, they are not shops
NON_SHOP_FILES = {"_manifest.json", "_run_summary.json"}

# the four cards at the top of the panel
SUMMARY_CARDS = {
    "Total e-Transaction": "total_etransactions",
    "Aadhaar Authenticated": "aadhaar_authenticated",
    "Other Mode Authenticated": "other_mode_authenticated",
    "Non-Authenticated": "non_authenticated",
}

# the two transaction tables share these row labels, but not the exact spelling -
# one says "Priority Household(PHH)" and the other "Priority Household (PHH)",
# so rows are matched on a squashed version of the label (see match_row)
RATION_CARD_TYPES = {"Priority Household(PHH)": "phh",
                     "Antyodaya Anna Yojana (AAY)": "aay"}

COMMODITIES = {
    "Wheat": "wheat",
    "Rice": "rice",
    "Fortified Rice": "fortified_rice",
    "Coarse Grains": "coarse_grains",
    "Barley": "barley",
    "Bajra": "bajra",
    "Maize": "maize",
    "Jowar": "jowar",
    "Ragi": "ragi",
    "Kodo": "kodo",
    "Total": "all_commodities",  # the sites own total row, renamed so it reads clearly
}

# the two tables label their columns differently for the same thing
TXN_COLUMNS = {"Regular": "regular", "Intra State": "intra_state",
               "Inter State": "inter_state", "Total": "total"}
QTY_COLUMNS = {"Regular Txn": "regular", "Intra state Txn": "intra_state",
               "Inter State Txn": "inter_state", "Total": "total"}

# -- SMALL HELPERS --

def squash(label):
    """Turns 'Priority Household (PHH)' into 'priorityhouseholdphh' so spacing cant break a match"""
    return re.sub(r"[^a-z0-9]", "", label.lower())

def to_number(text):
    """Turns '3,890' into 3890, returns None if theres no number in there"""
    # the scraper already did this and stored the result, this is only the
    # fallback for when that stored value came back None
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

def match_row(table, wanted_label):
    """Finds a row by label ignoring spacing and punctuation, None if its not there"""
    if not table:
        return None
    target = squash(wanted_label)
    for label, cells in table.get("rows", {}).items():
        if squash(label) == target:
            return cells
    return None

def cell_number(cells, column):
    """Reads one cell as a number, None if the row or column is missing"""
    # every cell is {"raw": "3,890", "value": 3890}. we trust value, and only
    # re-parse raw if value is None, so a blank stays blank instead of becoming 0
    if not cells:
        return None
    cell = cells.get(column)
    if not cell:
        return None
    if cell.get("value") is not None:
        return cell["value"]
    return to_number(cell.get("raw"))

# -- FLATTENING --

def flatten_record(record):
    """Turns one shops nested json into a single flat dict of columns"""
    tables = record.get("tables", {})
    row = {
        "fps_id": record.get("fps_id"),
        "fps_name": record.get("fps_name"),
        "district": record.get("district"),
        "district_code": record.get("district_code"),
        "state": record.get("state"),
        "year": record.get("year"),
        "month": record.get("month"),
        "month_name": record.get("month_name"),
    }

    # 1 the four summary cards
    cards = record.get("summary_cards", {})
    for label, column in SUMMARY_CARDS.items():
        card = cards.get(label) or {}
        row[column] = card.get("value") if card.get("value") is not None else to_number(card.get("raw"))

    # 2 share of transactions done by aadhaar, the one derived number we add
    total_txn = row.get("total_etransactions")
    aadhaar = row.get("aadhaar_authenticated")
    if total_txn and aadhaar is not None:
        row["aadhaar_authenticated_pct"] = round(aadhaar * 100.0 / total_txn, 2)
    else:
        # blank rather than 0, because "no transactions" is not "0% aadhaar"
        row["aadhaar_authenticated_pct"] = None

    # 3 the two ration card tables, same shape so the same loop does both
    for table_key, suffix in (("number_of_transaction", "txn"),
                              ("number_of_transacted_ration_card", "ration_card")):
        table = tables.get(table_key)
        for label, prefix in RATION_CARD_TYPES.items():
            cells = match_row(table, label)
            for column_label, column in TXN_COLUMNS.items():
                row[f"{prefix}_{column}_{suffix}"] = cell_number(cells, column_label)

    # 4 distributed quantity, 11 commodity rows x 4 columns
    quantity = tables.get("distributed_quantity_kg")
    for label, prefix in COMMODITIES.items():
        cells = match_row(quantity, label)
        for column_label, column in QTY_COLUMNS.items():
            row[f"{prefix}_{column}_kg"] = cell_number(cells, column_label)

    return row

# -- CHECKS --

def quality_flags(record, row):
    """Lists whats odd about this shop, empty list when nothing is"""
    flags = []

    # a table that didnt load. never seen in practice but it is the failure the
    # brief asks about, and a missing table would otherwise look like real zeros
    for table_key in ("number_of_transaction", "number_of_transacted_ration_card",
                      "distributed_quantity_kg"):
        if not record.get("tables", {}).get(table_key):
            flags.append(f"missing_table:{table_key}")

    # a real shop that sold nothing this month, valid data, just not comparable
    if row.get("total_etransactions") == 0:
        flags.append("no_transactions")

    # the four cards should add up to the headline number
    parts = [row.get(c) for c in ("aadhaar_authenticated", "other_mode_authenticated",
                                  "non_authenticated")]
    if row.get("total_etransactions") is not None and all(p is not None for p in parts):
        if sum(parts) != row["total_etransactions"]:
            flags.append("cards_dont_sum")

    # the sites own Total row should equal wheat + rice + coarse grains
    total_row = row.get("all_commodities_total_kg")
    parts = [row.get(c) for c in ("wheat_total_kg", "rice_total_kg", "coarse_grains_total_kg")]
    if total_row is not None and all(p is not None for p in parts):
        if abs(sum(parts) - total_row) > 0.01:
            flags.append("commodity_total_mismatch")

    return flags

def find_raw_files():
    """Every shop json under data/raw, sorted so the csv comes out in a stable order"""
    paths = []
    for folder, _, filenames in os.walk(RAW_DIR):
        for filename in sorted(filenames):
            if filename.endswith(".json") and filename not in NON_SHOP_FILES:
                paths.append(os.path.join(folder, filename))
    return sorted(paths)

def count_expected_shops():
    """Reads every _manifest.json to get how many shops each folder should have"""
    # the manifest is what the site itself listed, so it is the only honest
    # answer to "did we get everything" (S4.3)
    expected = {}
    for folder, _, filenames in os.walk(RAW_DIR):
        if "_manifest.json" not in filenames:
            continue
        with open(os.path.join(folder, "_manifest.json"), encoding="utf-8") as handle:
            expected[folder] = len(json.load(handle))
    return expected

# -- MAIN --

def main():
    """Reads every raw shop file and writes one flat csv. Exit code 0 clean, 1 if anything was off"""
    # this prints nothing - the csv is the deliverable. anything worth knowing is
    # in it already: quality_flags per row, and the exit code for the run
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    paths = find_raw_files()
    if not paths:
        return 1

    rows, flagged, unreadable = [], 0, 0
    for path in paths:
        # a raw file that wont parse is skipped rather than fatal, one bad file
        # should not cost the whole csv
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            unreadable += 1
            continue

        row = flatten_record(record)
        flags = quality_flags(record, row)
        row["quality_flags"] = ";".join(flags)
        if flags:
            flagged += 1
        rows.append(row)

    # sort so the csv reads month, then district, then shop
    rows.sort(key=lambda r: (r["year"], r["month"], r["district_code"], r["fps_id"]))

    # column order comes from the first row, which is built in a fixed order, so
    # the csv columns stay the same between runs
    columns = list(rows[0].keys())
    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    # completeness: the manifests say how many shops the site listed, so a short
    # csv is a fact we can state rather than something you have to notice (S4.3)
    total_expected = sum(count_expected_shops().values())
    return 1 if (flagged or unreadable or len(rows) != total_expected) else 0

if __name__ == "__main__":
    # same as the scraper, a run that found problems shouldnt exit as success
    sys.exit(main())
