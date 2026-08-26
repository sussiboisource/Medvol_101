"""Data-access layer. The only module that reads C:/.../data/ directly.

Discovers raw order-line files (.csv / .xlsx), loads and stacks them, dedupes rows that
appear in more than one overlapping file, and loads the three reference files: the
new-products list (item_brand_mapping.csv, powers Tab 3), the brand master
(brand_master.csv, powers Tab 2's Brand join), and the new-counters log
(New Medvol customers ....csv, powers Tab 1's New/Old split). Everything else in the
project should call functions here instead of touching data/ itself.
"""

import re
import time
import warnings
from pathlib import Path

import pandas as pd

import config

# Every problem the pipeline hit while running -- file read failures, severe schema mismatches,
# duplicate keys, etc. Collected here so build_report_data.py can put them in the JSON and the
# dashboard can show them directly, instead of them only ever appearing in the terminal (which
# is exactly how "some data got silently missed" goes unnoticed).
BUILD_ISSUES = []


def report_issue(severity, message, action=None):
    """severity: 'error' (some real data was skipped/lost), 'warning' (handled, but worth a
    look), or 'info' (a fact about this build worth stating, not a problem).

    `action` is the concrete next step -- the thing to actually DO about it. An issue nobody can
    act on is just noise, so anything worth reporting should be able to say what to do about it.
    Always also prints immediately, so the terminal output stays informative too."""
    BUILD_ISSUES.append({"severity": severity, "message": message, "action": action})
    print(f"  {severity.upper()}: {message}", flush=True)
    if action:
        print(f"    -> WHAT TO DO: {action}", flush=True)


def progress_bar(current, total, width=24):
    """Text progress bar, e.g. '[################--------] 16/24'. No external deps."""
    filled = int(width * current / total) if total else width
    return "[" + "#" * filled + "-" * (width - filled) + f"] {current}/{total}"


def strip_string_columns(df):
    """Different export batches (different people, different times) can leave stray
    whitespace on cells -- that silently breaks exact-match joins and status comparisons.
    Strip every string column so 'DR123 ' and 'DR123' are treated as the same value."""
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].str.strip()
    return df


EXCEL_DATE_ORIGIN = "1899-12-30"


def parse_dates(series):
    """Parses a date column that may contain either normal date strings (from .xlsx/.csv,
    read via openpyxl -- date-formatted cells come back as real datetimes, stringified into
    something pd.to_datetime already understands) OR raw Excel serial-number strings (from
    .xlsb via pyxlsb, which does NOT auto-convert date-formatted cells like openpyxl does --
    it hands back the underlying day-count number, e.g. "45673.5", which under dtype=str
    becomes a plain numeric string pd.to_datetime can't recognize as a date at all). Without
    this fallback, every date in every .xlsb file silently becomes unparseable."""
    parsed = pd.to_datetime(series, errors="coerce")
    unresolved = parsed.isna() & series.notna()
    if unresolved.any():
        serials = pd.to_numeric(series[unresolved], errors="coerce")
        parsed.loc[unresolved] = pd.to_datetime(serials, unit="D", origin=EXCEL_DATE_ORIGIN, errors="coerce")
    return parsed


MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

FILENAME_PERIOD_RE = re.compile(
    r"([A-Za-z]+)'(\d{2})(?:\s*(?:to|-)\s*([A-Za-z]+)'(\d{2}))?", re.IGNORECASE
)


def _month_token_to_period(month_name, two_digit_year):
    month_num = MONTH_NUMBERS.get(month_name.lower())
    if month_num is None:
        return None
    year = 2000 + int(two_digit_year)
    return pd.Period(freq="M", year=year, month=month_num)


def parse_period_from_filename(filename):
    """Best-effort parse of the real naming convention, e.g.
    "Order Details Apr'23 to June'23.xlsx" or "Order Details Dec'25.xlsx".
    Returns (period_start_month, period_end_month) as pandas Periods, or (None, None)."""
    match = FILENAME_PERIOD_RE.search(filename)
    if not match:
        return None, None
    start_month, start_year, end_month, end_year = match.groups()
    start_period = _month_token_to_period(start_month, start_year)
    if start_period is None:
        return None, None
    if end_month and end_year:
        end_period = _month_token_to_period(end_month, end_year)
        if end_period is None:
            end_period = start_period
    else:
        end_period = start_period
    return start_period, end_period


def load_file_periods_manifest():
    """Manual overrides/additions: filename -> (period_start, period_end). Optional file;
    only needed for filenames the auto-parser can't handle."""
    if not config.FILE_PERIODS_MANIFEST.exists():
        return {}
    manifest_df = pd.read_csv(config.FILE_PERIODS_MANIFEST, dtype=str)
    manifest = {}
    for _, row in manifest_df.iterrows():
        manifest[row["filename"]] = (
            pd.Period(row["period_start"], freq="M"),
            pd.Period(row["period_end"], freq="M"),
        )
    return manifest


def _is_reference_file(filename):
    lower = filename.lower()
    return any(lower.startswith(prefix.lower()) for prefix in config.NON_ORDER_FILE_PREFIXES)


def _is_editor_lock_file(filename):
    """Excel/LibreOffice lock files ('~$Foo.xlsx') sit next to the real file whenever it's open
    in the editor. They hold no data, and Excel keeps them locked, so trying to read one throws
    PermissionError. Skipping them silently is correct -- reporting them as missed data is not."""
    lower = filename.lower()
    return any(lower.startswith(prefix.lower()) for prefix in config.IGNORED_FILENAME_PREFIXES)


def discover_order_files():
    """Every .csv/.xlsx/.xlsb in data/ that isn't a known reference file. Returns
    (found, skipped): found is a list of dicts {path, filename, period_start, period_end,
    period_source}; skipped is a list of filenames with no parseable period."""
    if not config.DATA_DIR.exists():
        return [], []  # callers unpack (found, skipped) -- a bare [] would raise here

    manifest = load_file_periods_manifest()
    found = []
    skipped = []
    lock_files_ignored = []

    for path in sorted(config.DATA_DIR.iterdir()):
        if path.suffix.lower() not in (".csv", ".xlsx", ".xlsb"):
            continue
        if _is_editor_lock_file(path.name):
            lock_files_ignored.append(path.name)
            continue
        if _is_reference_file(path.name):
            continue

        if path.name in manifest:
            period_start, period_end = manifest[path.name]
            period_source = "manifest"
        else:
            period_start, period_end = parse_period_from_filename(path.name)
            period_source = "filename" if period_start is not None else None

        if period_start is None:
            skipped.append(path.name)
            continue

        found.append({
            "path": path,
            "filename": path.name,
            "period_start": period_start,
            "period_end": period_end,
            "period_source": period_source,
        })

    if lock_files_ignored:
        print(
            f"  Note: ignored {len(lock_files_ignored)} Excel lock file(s) (no data in them, "
            f"they just mean the real file is open in Excel): {lock_files_ignored}",
            flush=True,
        )
    if skipped:
        warnings.warn(
            f"Skipped {len(skipped)} file(s) in data/ with no parseable period and no "
            f"file_periods.csv entry: {skipped}"
        )

    return found, skipped


def _read_one_file(path):
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return pd.read_excel(path, dtype=str)
    if suffix == ".xlsb":
        return pd.read_excel(path, dtype=str, engine="pyxlsb")

    last_error = None
    for encoding in config.CSV_ENCODING_FALLBACKS:
        try:
            return pd.read_csv(path, dtype=str, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise last_error


# Which metric silently breaks if a given critical column goes missing -- so the warning can say
# what actually goes wrong instead of just naming the column.
COLUMN_IMPACT = {
    "Order_Number": "duplicate detection across overlapping files",
    "OrdPlaced_Date": "the transaction date (falls back to Order_InitiatedDate)",
    "Order_InitiatedDate": "the fallback transaction date, used when OrdPlaced_Date is blank",
    "Doctor_Code": "the New/Old counter split",
    "Item_Code": "SKU grouping, and both reference-file joins",
    "Item_Description": "SKU names in the tables",
    "Division_Name": "every Division filter and the whole Division Trend tab",
    "Quantity": "Gross Sales (PTR x Quantity), so Medvol % and Net Sales %",
    "Amount": "ALL sales figures on every tab",
    "InvoiceAmount": "Net Sales % on the Division Trend tab",
    "Order_Status": "valid/excluded row classification -- rows can't be filtered at all",
    "PTR": "Gross Sales, so Medvol % and Net Sales %",
    "DiscountOnPTR": "the 'DiscountOnPTR only' discount buckets",
    "Cash_Discount": "the compounded 'Total discount' buckets",
}


def _check_columns(filename, df):
    """Reports missing columns by IMPACT, not by count. A file missing a column no logic reads
    is fine and shouldn't cost the user a moment's attention; a file missing 'Amount' is a
    five-alarm fire. Previously both looked identical in the output."""
    present = set(df.columns)
    missing_critical = [c for c in config.CRITICAL_COLUMNS if c not in present]
    missing_other = (set(config.EXPECTED_COLUMNS) - present
                     - set(config.CRITICAL_COLUMNS) - config.KNOWN_OPTIONAL_COLUMNS)

    if len(missing_critical) >= len(config.CRITICAL_COLUMNS) / 2:
        report_issue(
            "error",
            f"MOST/ALL COLUMNS MISSING in '{filename}' (found columns: {list(df.columns)}) -- "
            f"likely wrong sheet, a shifted header row, or an empty file. Its data is probably "
            f"NOT correctly included in this report.",
            action=f"Open '{filename}' and check that row 1 is the real header row and that the "
                   f"data is on the first sheet.",
        )
    elif missing_critical:
        impacts = "; ".join(f"{c} (breaks {COLUMN_IMPACT.get(c, 'part of the report')})"
                            for c in missing_critical)
        report_issue(
            "error",
            f"'{filename}' is missing {len(missing_critical)} column(s) the report actually "
            f"uses: {impacts}. Rows from this file will be blank/zero for those metrics.",
            action=f"Re-export '{filename}' with these columns included: "
                   f"{', '.join(missing_critical)}.",
        )

    if missing_other:
        report_issue(
            "warning",
            f"'{filename}' is missing {sorted(missing_other)} -- no calculation in this report "
            f"reads those, so no number is affected. Noting it only in case the export changed "
            f"shape unexpectedly.",
            action="No action needed unless you expected those columns to be there.",
        )


def load_order_lines():
    """Loads every discovered order file, stacks them, dedupes rows that appear identically
    in more than one overlapping file. Returns (DataFrame, meta_dict)."""
    file_infos, skipped_files = discover_order_files()
    total_files = len(file_infos)

    frames = []
    files_failed = []
    for i, info in enumerate(file_infos, start=1):
        print(f"{progress_bar(i - 1, total_files)} reading {info['filename']} ...", flush=True)
        t0 = time.perf_counter()
        try:
            df = _read_one_file(info["path"])
        except Exception as exc:
            report_issue(
                "error",
                f"MISSED ENTIRE FILE '{info['filename']}' -- could not be read at all "
                f"({type(exc).__name__}: {exc}). None of its data is included in this report.",
                action=("Close the file in Excel and rerun -- Excel locks a workbook while it's "
                        "open." if isinstance(exc, PermissionError) else
                        f"Open '{info['filename']}' and check it isn't corrupt or password-protected."),
            )
            files_failed.append(info["filename"])
            continue
        elapsed = time.perf_counter() - t0
        _check_columns(info["filename"], df)
        df["_source_file"] = info["filename"]
        df["_period_start"] = str(info["period_start"])
        df["_period_end"] = str(info["period_end"])
        frames.append(df)
        print(f"{progress_bar(i, total_files)} done: {len(df):,} rows in {elapsed:.1f}s", flush=True)

    if not frames:
        return pd.DataFrame(columns=config.EXPECTED_COLUMNS), {
            "files_loaded": [], "files_skipped": skipped_files, "files_failed": files_failed,
            "duplicate_rows_dropped": 0, "duplicate_rows_conflicting": 0,
            "conflicting_keys_sample": [], "conflict_policy": config.DUPLICATE_CONFLICT_POLICY,
            "conflict_extra_rows": 0, "conflict_extra_amount": 0.0, "conflict_file_pairs": [],
        }

    print("Sanitizing: stripping stray whitespace (different export batches format cells differently) ...", flush=True)
    t0 = time.perf_counter()
    frames = [strip_string_columns(f) for f in frames]
    print(f"  done in {time.perf_counter() - t0:.1f}s", flush=True)

    print("Combining files and checking for duplicate rows across overlapping files ...", flush=True)
    t0 = time.perf_counter()
    combined = pd.concat(frames, ignore_index=True)

    dup_key = [config.SKU_ID_COLUMN, "Order_Number"]
    value_check_cols = ["Amount", "InvoiceAmount", "Quantity", "DiscountOnPTR"]

    # Which file covers the latest months -- used by the "keep_latest" conflict policy, and to
    # describe the overlap in plain language either way.
    file_recency = {info["filename"]: str(info["period_end"]) for info in file_infos}

    # IMPORTANT: (Item_Code, Order_Number) is NOT guaranteed unique within a single file -- the
    # same SKU can appear as two separate lines on one order (different batch, scheme, or
    # stockist). So "keep one row per key" is the wrong rule: it would silently delete a
    # legitimate second line. The right rule is "keep every row from ONE file, drop the other
    # files' copies" -- which collapses to keeping 1 row in the ordinary 1-line-per-file case,
    # and correctly keeps both lines when an order really does list a SKU twice.
    def _keep_only_latest_file(group):
        latest = max(group["_source_file"].unique(), key=lambda f: file_recency.get(f, ""))
        return group.index[group["_source_file"] != latest].tolist()

    conflicting_keys = []
    conflict_file_pairs = {}
    conflict_extra_rows = 0
    conflict_extra_amount = 0.0
    rows_to_drop = []
    for key, group in combined.groupby(dup_key):
        if len(group) <= 1:
            continue
        if group["_source_file"].nunique() <= 1:
            continue  # repeated key within one file's own data -- a real multi-line order, keep it
        distinct_value_rows = group[value_check_cols].drop_duplicates()
        if len(distinct_value_rows) > 1:
            conflicting_keys.append(key)
            files_involved = tuple(sorted(group["_source_file"].unique()))
            conflict_file_pairs[files_involved] = conflict_file_pairs.get(files_involved, 0) + 1
            # What keeping every copy actually costs. Measured against the copy set we WOULD
            # keep (the latest file's rows), not against a single row -- otherwise a legitimate
            # two-line order would be reported as if one of its lines were duplicate money.
            latest = max(group["_source_file"].unique(), key=lambda f: file_recency.get(f, ""))
            kept = group[group["_source_file"] == latest]
            amounts = pd.to_numeric(group["Amount"], errors="coerce").fillna(0.0)
            kept_amounts = pd.to_numeric(kept["Amount"], errors="coerce").fillna(0.0)
            conflict_extra_rows += len(group) - len(kept)
            conflict_extra_amount += float(amounts.sum() - kept_amounts.sum())
            if config.DUPLICATE_CONFLICT_POLICY == "keep_latest":
                rows_to_drop.extend(_keep_only_latest_file(group))
            continue
        # Values agree across files, so the copies are interchangeable -- but keep the whole
        # row set from one file rather than a single row, for the multi-line reason above.
        rows_to_drop.extend(_keep_only_latest_file(group))

    deduped = combined.drop(index=rows_to_drop)
    print(f"  done in {time.perf_counter() - t0:.1f}s ({len(deduped):,} rows after dedup)", flush=True)

    if skipped_files:
        report_issue(
            "error",
            f"MISSED {len(skipped_files)} FILE(S) -- no parseable period in the filename and no "
            f"file_periods.csv entry, so they were never even opened: {skipped_files}. None of "
            f"their data is included in this report.",
            action=f"Either rename them to match the \"Apr'23 to June'23\" pattern, or add a row "
                   f"to data/file_periods.csv giving each one a period_start and period_end.",
        )
    if conflicting_keys:
        overlap_desc = "; ".join(
            f"{' + '.join(files)}: {count:,} line(s)"
            for files, count in sorted(conflict_file_pairs.items(), key=lambda kv: -kv[1])[:5]
        )
        if config.DUPLICATE_CONFLICT_POLICY == "keep_latest":
            report_issue(
                "info",
                f"{len(conflicting_keys):,} order line(s) appear in more than one file with "
                f"DIFFERENT values. Policy is 'keep_latest', so the copy from the newest export "
                f"was kept and {conflict_extra_rows:,} older copies (Rs {conflict_extra_amount:,.0f}) "
                f"were dropped. Overlaps: {overlap_desc}",
                action="Set DUPLICATE_CONFLICT_POLICY = \"keep_all\" in scripts/config.py if you "
                       "would rather keep every copy and reconcile by hand.",
            )
        else:
            report_issue(
                "error",
                f"DOUBLE-COUNTING: {len(conflicting_keys):,} order line(s) appear in more than one "
                f"file with DIFFERENT Amount/Quantity/discount values (a later export usually "
                f"restates an earlier order). Policy is 'keep_all', so every copy is being counted "
                f"-- inflating totals by {conflict_extra_rows:,} extra line(s), about "
                f"Rs {conflict_extra_amount:,.0f}. Overlaps: {overlap_desc}",
                action="Set DUPLICATE_CONFLICT_POLICY = \"keep_latest\" in scripts/config.py and "
                       "rerun to keep only the newest copy of each line, or run "
                       "scripts/inspect_duplicates.py to review them by hand first.",
            )

    loaded_filenames = [info["filename"] for info in file_infos if info["filename"] not in files_failed]
    meta = {
        "files_loaded": loaded_filenames,
        "files_skipped": skipped_files,
        "files_failed": files_failed,
        "duplicate_rows_dropped": len(rows_to_drop),
        "duplicate_rows_conflicting": len(conflicting_keys),
        "conflicting_keys_sample": conflicting_keys[:10],
        "conflict_policy": config.DUPLICATE_CONFLICT_POLICY,
        "conflict_extra_rows": conflict_extra_rows,
        "conflict_extra_amount": round(conflict_extra_amount, 2),
        "conflict_file_pairs": [
            {"files": list(files), "lines": count}
            for files, count in sorted(conflict_file_pairs.items(), key=lambda kv: -kv[1])[:10]
        ],
    }
    return deduped, meta


def load_new_products():
    """Reads item_brand_mapping.csv -- the NEW-PRODUCTS list (powers Tab 3), not a general
    brand map. Returns (DataFrame, meta)."""
    paths = sorted(
        p for p in config.DATA_DIR.glob(f"{config.NEW_PRODUCTS_FILE_PREFIX}*")
        if p.suffix.lower() in (".csv", ".xlsx")
    ) if config.DATA_DIR.exists() else []

    if not paths:
        return pd.DataFrame(columns=[
            config.NEW_PRODUCTS_SKU_COLUMN, config.NEW_PRODUCTS_BRAND_COLUMN,
            config.NEW_PRODUCTS_DIVISION_COLUMN, config.NEW_PRODUCTS_VERTICAL_COLUMN,
        ]), {"new_products_files": [], "duplicate_new_product_codes": 0}

    frames = []
    for p in paths:
        try:
            frames.append(strip_string_columns(_read_one_file(p)))
        except Exception as exc:
            report_issue("error", f"MISSED new-products file '{p.name}' -- could not be read ({type(exc).__name__}: {exc}).")
    if not frames:
        return pd.DataFrame(columns=[
            config.NEW_PRODUCTS_SKU_COLUMN, config.NEW_PRODUCTS_BRAND_COLUMN,
            config.NEW_PRODUCTS_DIVISION_COLUMN, config.NEW_PRODUCTS_VERTICAL_COLUMN,
        ]), {"new_products_files": [], "duplicate_new_product_codes": 0}
    combined = pd.concat(frames, ignore_index=True)

    dupe_mask = combined.duplicated(subset=[config.NEW_PRODUCTS_SKU_COLUMN], keep="first")
    duplicate_count = int(dupe_mask.sum())
    if duplicate_count:
        report_issue(
            "warning",
            f"{duplicate_count} duplicate '{config.NEW_PRODUCTS_SKU_COLUMN}' entries in the "
            f"new-products file(s) -- keeping first occurrence of each, dropping the rest."
        )
    deduped = combined[~dupe_mask]

    return deduped, {
        "new_products_files": [p.name for p in paths],
        "duplicate_new_product_codes": duplicate_count,
    }


def load_brand_master():
    """Reads brand_master.csv -- the comprehensive old+new SKU->Brand lookup for Tab 2's Brand
    join. Returns (DataFrame, meta). Empty DataFrame if the file doesn't exist yet or is a
    header-only placeholder."""
    if not config.BRAND_MASTER_FILE.exists():
        return pd.DataFrame(columns=[config.BRAND_MASTER_SKU_COLUMN, config.BRAND_MASTER_BRAND_COLUMN]), {
            "brand_master_file_present": False, "duplicate_brand_master_codes": 0,
        }

    try:
        df = strip_string_columns(_read_one_file(config.BRAND_MASTER_FILE))
    except Exception as exc:
        report_issue("error", f"MISSED brand_master.csv -- could not be read ({type(exc).__name__}: {exc}). Every SKU will show as Unmapped.")
        return pd.DataFrame(columns=[config.BRAND_MASTER_SKU_COLUMN, config.BRAND_MASTER_BRAND_COLUMN]), {
            "brand_master_file_present": False, "duplicate_brand_master_codes": 0,
        }
    dupe_mask = df.duplicated(subset=[config.BRAND_MASTER_SKU_COLUMN], keep="first")
    duplicate_count = int(dupe_mask.sum())
    if duplicate_count:
        report_issue(
            "warning",
            f"{duplicate_count} duplicate '{config.BRAND_MASTER_SKU_COLUMN}' entries in "
            f"brand_master.csv -- keeping first occurrence of each."
        )
    return df[~dupe_mask], {
        "brand_master_file_present": True,
        "duplicate_brand_master_codes": duplicate_count,
    }


def load_new_counters():
    """Reads the counter registration log (Allocated_CounterCode + Request_CreatedDate).
    Returns (DataFrame or None, meta). None means the file doesn't exist yet -- every counter
    is 'Old' by default in that case."""
    if not config.NEW_COUNTERS_FILE.exists():
        print(f"  New-counters file not found at: {config.NEW_COUNTERS_FILE} -- every counter will be 'Old'.", flush=True)
        return None, {"new_counters_file_present": False, "duplicate_counter_codes": 0}

    try:
        df = strip_string_columns(_read_one_file(config.NEW_COUNTERS_FILE))
    except Exception as exc:
        report_issue("error", f"MISSED new-counters file -- could not be read ({type(exc).__name__}: {exc}). Every counter will show as 'Old'.")
        return None, {"new_counters_file_present": False, "duplicate_counter_codes": 0}
    dupe_mask = df.duplicated(subset=[config.NEW_COUNTERS_ID_COLUMN], keep="first")
    duplicate_count = int(dupe_mask.sum())
    if duplicate_count:
        report_issue(
            "warning",
            f"{duplicate_count} duplicate '{config.NEW_COUNTERS_ID_COLUMN}' entries in the new "
            f"counters file -- keeping first occurrence of each."
        )
    deduped = df[~dupe_mask]
    parsed_dates = pd.to_datetime(deduped[config.NEW_COUNTERS_DATE_COLUMN], errors="coerce")
    unparseable = int(parsed_dates.isna().sum())
    print(
        f"  New-counters file found: {len(deduped):,} rows, {len(deduped) - unparseable:,} with a "
        f"parseable '{config.NEW_COUNTERS_DATE_COLUMN}' date"
        + (f" -- {unparseable:,} could NOT be parsed (check the date format)." if unparseable else "."),
        flush=True,
    )
    return deduped, {
        "new_counters_file_present": True,
        "duplicate_counter_codes": duplicate_count,
    }


def join_brand_master(orders_df, brand_master_df):
    """Adds a Brand column from brand_master.csv (Tab 2's join). Unmatched Item_Codes get
    config.UNMAPPED_BRAND_LABEL, never dropped or silently left blank."""
    result = orders_df.copy()
    if brand_master_df.empty:
        result["Brand"] = config.UNMAPPED_BRAND_LABEL
        return result

    lookup = brand_master_df.set_index(config.BRAND_MASTER_SKU_COLUMN)[config.BRAND_MASTER_BRAND_COLUMN]
    result["Brand"] = result[config.SKU_ID_COLUMN].map(lookup).fillna(config.UNMAPPED_BRAND_LABEL)
    return result


def join_counter_age(orders_df, new_counters_df, cutoff_date):
    """Adds a Counter_Age column ('Old' / 'New'), computed once at build time. A counter is
    'New' if it's in the new-counters file with a creation date on/after cutoff_date; every
    other counter (including any not in the file at all) is 'Old'. If new_counters_df is None
    or cutoff_date is None, every counter is 'Old'."""
    result = orders_df.copy()
    if new_counters_df is None or cutoff_date is None:
        result["Counter_Age"] = "Old"
        return result

    dates = pd.to_datetime(new_counters_df[config.NEW_COUNTERS_DATE_COLUMN], errors="coerce")
    lookup = pd.Series(dates.values, index=new_counters_df[config.NEW_COUNTERS_ID_COLUMN])
    creation_dates = result[config.COUNTER_ID_COLUMN].map(lookup)

    is_new = creation_dates.notna() & (creation_dates >= pd.to_datetime(cutoff_date))
    result["Counter_Age"] = "Old"
    result.loc[is_new, "Counter_Age"] = "New"
    return result


def new_product_sku_set(new_products_df):
    """The set of Item_Codes considered 'new products' for Tab 3, from item_brand_mapping.csv."""
    if new_products_df.empty:
        return set()
    return set(new_products_df[config.NEW_PRODUCTS_SKU_COLUMN].dropna())


