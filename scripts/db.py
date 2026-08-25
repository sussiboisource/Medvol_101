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


def discover_order_files():
    """Every .csv/.xlsx in data/ that isn't a known reference file. Returns list of dicts:
    {path, filename, period_start, period_end, period_source}."""
    if not config.DATA_DIR.exists():
        return []

    manifest = load_file_periods_manifest()
    found = []
    skipped = []

    for path in sorted(config.DATA_DIR.iterdir()):
        if path.suffix.lower() not in (".csv", ".xlsx", ".xlsb"):
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


def load_order_lines():
    """Loads every discovered order file, stacks them, dedupes rows that appear identically
    in more than one overlapping file. Returns (DataFrame, meta_dict)."""
    file_infos, skipped_files = discover_order_files()
    total_files = len(file_infos)

    frames = []
    for i, info in enumerate(file_infos, start=1):
        print(f"{progress_bar(i - 1, total_files)} reading {info['filename']} ...", flush=True)
        t0 = time.perf_counter()
        df = _read_one_file(info["path"])
        elapsed = time.perf_counter() - t0
        missing_cols = set(config.EXPECTED_COLUMNS) - set(df.columns)
        if missing_cols:
            severity = "MOST/ALL COLUMNS MISSING -- likely wrong sheet, shifted header, or empty file" \
                if len(missing_cols) > len(config.EXPECTED_COLUMNS) / 2 else "some columns missing"
            print(f"  WARNING [{severity}] {info['filename']}: found columns = {list(df.columns)}", flush=True)
            warnings.warn(f"{info['filename']} is missing expected columns: {missing_cols}")
        df["_source_file"] = info["filename"]
        df["_period_start"] = str(info["period_start"])
        df["_period_end"] = str(info["period_end"])
        frames.append(df)
        print(f"{progress_bar(i, total_files)} done: {len(df):,} rows in {elapsed:.1f}s", flush=True)

    if not frames:
        return pd.DataFrame(columns=config.EXPECTED_COLUMNS), {
            "files_loaded": [], "files_skipped": skipped_files,
            "duplicate_rows_dropped": 0, "duplicate_rows_conflicting": 0,
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

    conflicting_keys = []
    rows_to_drop = []
    for key, group in combined.groupby(dup_key):
        if len(group) <= 1:
            continue
        if group["_source_file"].nunique() <= 1:
            continue  # duplicate key within a single file's own data, not a file-overlap issue
        distinct_value_rows = group[value_check_cols].drop_duplicates()
        if len(distinct_value_rows) > 1:
            conflicting_keys.append(key)
            continue  # genuine disagreement -- keep all copies, flag it, don't guess
        rows_to_drop.extend(group.index[1:].tolist())

    deduped = combined.drop(index=rows_to_drop)
    print(f"  done in {time.perf_counter() - t0:.1f}s ({len(deduped):,} rows after dedup)", flush=True)

    meta = {
        "files_loaded": [info["filename"] for info in file_infos],
        "files_skipped": skipped_files,
        "duplicate_rows_dropped": len(rows_to_drop),
        "duplicate_rows_conflicting": len(conflicting_keys),
        "conflicting_keys_sample": conflicting_keys[:10],
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

    frames = [strip_string_columns(_read_one_file(p)) for p in paths]
    combined = pd.concat(frames, ignore_index=True)

    dupe_mask = combined.duplicated(subset=[config.NEW_PRODUCTS_SKU_COLUMN], keep="first")
    duplicate_count = int(dupe_mask.sum())
    if duplicate_count:
        warnings.warn(
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

    df = strip_string_columns(_read_one_file(config.BRAND_MASTER_FILE))
    dupe_mask = df.duplicated(subset=[config.BRAND_MASTER_SKU_COLUMN], keep="first")
    duplicate_count = int(dupe_mask.sum())
    if duplicate_count:
        warnings.warn(
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

    df = strip_string_columns(_read_one_file(config.NEW_COUNTERS_FILE))
    dupe_mask = df.duplicated(subset=[config.NEW_COUNTERS_ID_COLUMN], keep="first")
    duplicate_count = int(dupe_mask.sum())
    if duplicate_count:
        warnings.warn(
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


