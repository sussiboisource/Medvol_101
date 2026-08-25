"""Data-access layer. The only module that reads C:/.../data/ directly.

Discovers raw order-line files (.csv / .xlsx), loads and stacks them, dedupes rows that
appear in more than one overlapping file, and joins in the two reference files (item->brand
mapping, counter creation dates). Everything else in the project should call functions here
instead of touching data/ itself.
"""

import re
import warnings
from pathlib import Path

import pandas as pd

import config


MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

FILENAME_PERIOD_RE = re.compile(
    r"([A-Za-z]+)'(\d{2})(?:\s*to\s*([A-Za-z]+)'(\d{2}))?", re.IGNORECASE
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
    return any(lower.startswith(prefix) for prefix in config.NON_ORDER_FILE_PREFIXES)


def discover_order_files():
    """Every .csv/.xlsx in data/ that isn't a known reference file. Returns list of dicts:
    {path, filename, period_start, period_end, period_source}."""
    if not config.DATA_DIR.exists():
        return []

    manifest = load_file_periods_manifest()
    found = []
    skipped = []

    for path in sorted(config.DATA_DIR.iterdir()):
        if path.suffix.lower() not in (".csv", ".xlsx"):
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
    if path.suffix.lower() == ".xlsx":
        return pd.read_excel(path, dtype=str)

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

    frames = []
    for info in file_infos:
        df = _read_one_file(info["path"])
        missing_cols = set(config.EXPECTED_COLUMNS) - set(df.columns)
        if missing_cols:
            warnings.warn(f"{info['filename']} is missing expected columns: {missing_cols}")
        df["_source_file"] = info["filename"]
        df["_period_start"] = str(info["period_start"])
        df["_period_end"] = str(info["period_end"])
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=config.EXPECTED_COLUMNS), {
            "files_loaded": [], "files_skipped": skipped_files,
            "duplicate_rows_dropped": 0, "duplicate_rows_conflicting": 0,
        }

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

    meta = {
        "files_loaded": [info["filename"] for info in file_infos],
        "files_skipped": skipped_files,
        "duplicate_rows_dropped": len(rows_to_drop),
        "duplicate_rows_conflicting": len(conflicting_keys),
        "conflicting_keys_sample": conflicting_keys[:10],
    }
    return deduped, meta


def load_brand_mapping():
    """Concatenates every item_brand_mapping*.csv/.xlsx in data/. Returns (DataFrame, meta)."""
    paths = sorted(
        p for p in config.DATA_DIR.glob("item_brand_mapping*")
        if p.suffix.lower() in (".csv", ".xlsx")
    ) if config.DATA_DIR.exists() else []

    if not paths:
        return pd.DataFrame(columns=[
            config.BRAND_MAPPING_SKU_COLUMN, config.BRAND_MAPPING_BRAND_COLUMN,
            config.BRAND_MAPPING_DIVISION_COLUMN, config.BRAND_MAPPING_VERTICAL_COLUMN,
        ]), {"brand_mapping_files": [], "duplicate_brand_codes": 0}

    frames = [_read_one_file(p) for p in paths]
    combined = pd.concat(frames, ignore_index=True)

    dupe_mask = combined.duplicated(subset=[config.BRAND_MAPPING_SKU_COLUMN], keep="first")
    duplicate_count = int(dupe_mask.sum())
    if duplicate_count:
        warnings.warn(
            f"{duplicate_count} duplicate '{config.BRAND_MAPPING_SKU_COLUMN}' entries in brand "
            f"mapping file(s) -- keeping first occurrence of each, dropping the rest."
        )
    deduped = combined[~dupe_mask]

    return deduped, {
        "brand_mapping_files": [p.name for p in paths],
        "duplicate_brand_codes": duplicate_count,
    }


def load_counter_creation_dates():
    """Returns (DataFrame or None, meta). None means the file doesn't exist yet -- every
    counter should be treated as 'Old' by the caller in that case."""
    if not config.COUNTER_CREATION_DATES_FILE.exists():
        return None, {"counter_creation_dates_file_present": False, "duplicate_counter_codes": 0}

    df = _read_one_file(config.COUNTER_CREATION_DATES_FILE)
    dupe_mask = df.duplicated(subset=[config.COUNTER_ID_COLUMN], keep="first")
    duplicate_count = int(dupe_mask.sum())
    if duplicate_count:
        warnings.warn(
            f"{duplicate_count} duplicate '{config.COUNTER_ID_COLUMN}' entries in "
            f"counter_creation_dates.csv -- keeping first occurrence of each."
        )
    return df[~dupe_mask], {
        "counter_creation_dates_file_present": True,
        "duplicate_counter_codes": duplicate_count,
    }


def join_brand(orders_df, brand_df):
    """Adds Brand/Division_FromBrandFile/Vertical_FromBrandFile columns. Unmatched Item_Codes
    get config.UNMAPPED_BRAND_LABEL, never dropped or silently left blank."""
    if brand_df.empty:
        result = orders_df.copy()
        result["Brand"] = config.UNMAPPED_BRAND_LABEL
        return result

    lookup = brand_df.set_index(config.BRAND_MAPPING_SKU_COLUMN)[config.BRAND_MAPPING_BRAND_COLUMN]
    result = orders_df.copy()
    result["Brand"] = result[config.SKU_ID_COLUMN].map(lookup).fillna(config.UNMAPPED_BRAND_LABEL)
    return result


def join_counter_age(orders_df, counter_dates_df, cutoff_date):
    """Adds Counter_Age column: 'Old' / 'New' / config.UNKNOWN_COUNTER_AGE_LABEL. If
    counter_dates_df is None (no creation-date file yet) or cutoff_date is None, every
    counter is 'Old' -- the current, confirmed default."""
    result = orders_df.copy()
    if counter_dates_df is None or cutoff_date is None:
        result["Counter_Age"] = "Old"
        return result

    date_col = [c for c in counter_dates_df.columns if c != config.COUNTER_ID_COLUMN][0]
    lookup = counter_dates_df.set_index(config.COUNTER_ID_COLUMN)[date_col]
    creation_dates = result[config.COUNTER_ID_COLUMN].map(lookup)

    result["Counter_Age"] = config.UNKNOWN_COUNTER_AGE_LABEL
    known = creation_dates.notna()
    is_new = known & (pd.to_datetime(creation_dates) >= pd.to_datetime(cutoff_date))
    is_old = known & ~is_new
    result.loc[is_new, "Counter_Age"] = "New"
    result.loc[is_old, "Counter_Age"] = "Old"
    return result
