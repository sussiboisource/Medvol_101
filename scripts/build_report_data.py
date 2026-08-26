"""The one script you actually run. Pulls data via db.py, computes every derived column,
classifies rows, aggregates into the two tabs' shapes, and writes output/report_data.json --
then embeds that same JSON into dashboard.html so it also works opened directly (file://),
with no server needed.

Usage: python build_report_data.py
"""

import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

import config
import db


@contextmanager
def stage(label):
    """Prints a start/done line with elapsed time around a block, so a long-running step
    never goes silent -- that's what caused the "is it stuck?" confusion before."""
    print(f"{label} ...", flush=True)
    t0 = time.perf_counter()
    yield
    print(f"  done in {time.perf_counter() - t0:.1f}s", flush=True)

DASHBOARD_HTML_PATH = config.PROJECT_ROOT / "dashboard.html"
REPORT_DATA_SCRIPT_RE = re.compile(
    r'(<script type="application/json" id="report-data">)(.*?)(</script>)',
    re.DOTALL,
)


def embed_data_in_dashboard(report_json_str):
    if not DASHBOARD_HTML_PATH.exists():
        db.report_issue(
            "error",
            f"{DASHBOARD_HTML_PATH.name} does not exist at {DASHBOARD_HTML_PATH} -- no data was "
            f"embedded into it (there was nothing to embed it into). output/report_data.json was "
            f"still written and is current; the dashboard itself needs to exist first (git "
            f"checkout it back, or pull again) before rerunning this script."
        )
        return
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    safe_json = report_json_str.replace("</script>", "<\\/script>")
    new_html, count = REPORT_DATA_SCRIPT_RE.subn(
        lambda m: m.group(1) + safe_json + m.group(3), html
    )
    if count == 0:
        db.report_issue(
            "warning",
            f"Could not find the report-data <script> tag in {DASHBOARD_HTML_PATH.name} -- the "
            f"embedded copy was not updated. {DASHBOARD_HTML_PATH.name} will still work if served "
            f"over HTTP (output/report_data.json is current)."
        )
        return
    DASHBOARD_HTML_PATH.write_text(new_html, encoding="utf-8")


def to_numeric(series, fill_value=None):
    numeric = pd.to_numeric(series, errors="coerce")
    if fill_value is not None:
        numeric = numeric.fillna(fill_value)
    return numeric


_VALID_STATUSES_NORMALIZED = {" ".join(s.split()).strip().lower() for s in config.VALID_ORDER_STATUSES}


def classify_status(status):
    """Different export batches (different people, different times) can format the same
    status differently -- extra whitespace, different casing. Compare on a normalized form so
    "Fully Invoiced ", "fully  invoiced", etc. all still match, instead of silently falling
    into Unclassified."""
    normalized = " ".join(str(status).split()).strip().lower()
    if normalized in _VALID_STATUSES_NORMALIZED:
        return "Valid"
    if any(keyword in normalized for keyword in config.EXCLUDED_STATUS_KEYWORDS):
        return "Excluded"
    return "Unclassified"


def compute_canonical_date(df):
    primary = db.parse_dates(df[config.PRIMARY_DATE_COLUMN])
    fallback = db.parse_dates(df[config.FALLBACK_DATE_COLUMN])
    return primary.fillna(fallback)


def compute_fy_label(txn_date):
    """FY24 = Apr 2023 - Mar 2024 (label = the calendar year the FY *ends* in). Same convention
    used in dashboard.html's fyLabel() JS function and verify_data.py's independent_fy() --
    keep all three in sync if this ever changes. Rows with no parseable date get "Unknown"
    rather than being silently dropped or crashing on NaT."""
    fy_end_year = (txn_date.dt.year + (txn_date.dt.month >= 4).astype("Int64")).astype("Int64")
    labels = "FY" + (fy_end_year % 100).astype("Int64").astype(str).str.zfill(2)
    return labels.where(txn_date.notna(), "Unknown")


def canonicalize_labels(df):
    """The same Item_Code can carry slightly different Item_Description/Division_Name text
    across export batches (casing drift, e.g. "Ketorol DT" vs "Ketorol Dt"). Without this, the
    same SKU would silently split into multiple table rows. Pick the most common text per
    Item_Code and use it everywhere, so one SKU is always one row."""
    df = df.copy()
    for col in ["Item_Description", "Division_Name"]:
        mode_map = df.groupby(config.SKU_ID_COLUMN)[col].agg(
            lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0]
        )
        df[col] = df[config.SKU_ID_COLUMN].map(mode_map)

    # The per-SKU pass above stops one SKU from splitting, but Division_Name is a shared
    # dimension across many SKUs -- two DIFFERENT SKUs can each independently carry a
    # different casing of the same real division (e.g. "Aqura MS" vs "Aqura Ms"), which
    # would otherwise show up as two separate entries in the Division filter/entity lists.
    # Collapse case-only variants dataset-wide to whichever casing is most common overall.
    division_mode_by_lower = (
        df.groupby(df["Division_Name"].str.lower())["Division_Name"]
        .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0])
    )
    df["Division_Name"] = df["Division_Name"].str.lower().map(division_mode_by_lower)
    return df


def add_derived_columns(df):
    df = df.copy()
    df = canonicalize_labels(df)

    df["_status_class"] = df["Order_Status"].apply(classify_status)
    df["_txn_date"] = compute_canonical_date(df)
    df["_fy"] = compute_fy_label(df["_txn_date"])

    df["_ptr"] = to_numeric(df["PTR"], fill_value=0.0)
    df["_quantity"] = to_numeric(df["Quantity"], fill_value=0.0)
    df["_discount_on_ptr"] = to_numeric(df["DiscountOnPTR"], fill_value=0.0)
    df["_cash_discount"] = to_numeric(df["Cash_Discount"], fill_value=0.0)
    df["_amount"] = to_numeric(df["Amount"])
    df["_invoice_amount"] = to_numeric(df["InvoiceAmount"])

    df["_gross_sales"] = df["_ptr"] * df["_quantity"]
    df["_total_discount_pct"] = 100 * (
        1 - (1 - df["_discount_on_ptr"] / 100) * (1 - df["_cash_discount"] / 100)
    )

    df["_bucket_ptr_only"] = pd.cut(
        df["_discount_on_ptr"], bins=config.DISCOUNT_BUCKET_EDGES,
        labels=config.DISCOUNT_BUCKET_LABELS, right=False,
    )
    df["_bucket_total"] = pd.cut(
        df["_total_discount_pct"], bins=config.DISCOUNT_BUCKET_EDGES,
        labels=config.DISCOUNT_BUCKET_LABELS, right=False,
    )
    return df


def build_counter_tab(valid_df):
    """Grain: Counter_Age x FY x SKU x discount bucket. Counter_Age is pre-computed once at
    build time (config.COUNTER_AGE_CUTOFF_DATE) -- per-counter (Doctor_Code) granularity was
    tried and abandoned: at real data scale it produced 300k+ JSON records and made the
    dashboard unusable. FY is safe to add on top of that (only ~5 distinct values -- FY24-FY27
    plus "Unknown" for undated rows -- vs. thousands of individual counters)."""
    # Brand is deliberately excluded here -- only the Division Trend tab needs it. Fewer
    # group-by columns also means fewer distinct records, which matters at real data scale.
    records = []
    group_cols = ["Counter_Age", "_fy", config.SKU_ID_COLUMN, "Item_Description", "Division_Name"]

    for bucket_col, discount_type in (("_bucket_ptr_only", "ptr_only"), ("_bucket_total", "total")):
        grouped = (
            valid_df.groupby(group_cols + [bucket_col], observed=True)["_amount"]
            .agg(amount="sum", order_lines="count")
            .reset_index()
            .rename(columns={bucket_col: "bucket"})
        )
        grouped["discount_type"] = discount_type
        records.extend(grouped.to_dict(orient="records"))

    return [
        {
            "counter_age": r["Counter_Age"],
            "fy": r["_fy"],
            "item_code": r[config.SKU_ID_COLUMN],
            "item_description": r["Item_Description"],
            "division": r["Division_Name"],
            "discount_type": r["discount_type"],
            "bucket": str(r["bucket"]),
            "amount": round(float(r["amount"]), 2),
            "order_lines": int(r["order_lines"]),
        }
        for r in records
    ]


def build_np_discounts_tab(valid_df, new_product_skus):
    """Tab 3: same 9 discount buckets as Tab 1, but only for SKUs in the new-products list
    (item_brand_mapping.csv), at SKU level. No Brand -- see build_counter_tab."""
    df = valid_df[valid_df[config.SKU_ID_COLUMN].isin(new_product_skus)]
    records = []
    group_cols = ["_fy", config.SKU_ID_COLUMN, "Item_Description", "Division_Name"]

    for bucket_col, discount_type in (("_bucket_ptr_only", "ptr_only"), ("_bucket_total", "total")):
        grouped = (
            df.groupby(group_cols + [bucket_col], observed=True)["_amount"]
            .agg(amount="sum", order_lines="count")
            .reset_index()
            .rename(columns={bucket_col: "bucket"})
        )
        grouped["discount_type"] = discount_type
        records.extend(grouped.to_dict(orient="records"))

    return [
        {
            "fy": r["_fy"],
            "item_code": r[config.SKU_ID_COLUMN],
            "item_description": r["Item_Description"],
            "division": r["Division_Name"],
            "discount_type": r["discount_type"],
            "bucket": str(r["bucket"]),
            "amount": round(float(r["amount"]), 2),
            "order_lines": int(r["order_lines"]),
        }
        for r in records
    ]


def build_division_tab(valid_df):
    """Rows with no parseable transaction date (neither OrdPlaced_Date nor
    Order_InitiatedDate usable) can't be placed on the timeline -- excluded here rather than
    leaking a broken 'NaT' month value into the tab (that silently corrupted the FY/month
    grouping and crashed downstream tools expecting 'YYYY-MM')."""
    df = valid_df[valid_df["_txn_date"].notna()].copy()
    undated_rows = int(valid_df["_txn_date"].isna().sum())
    if undated_rows:
        db.report_issue(
            "warning",
            f"{undated_rows:,} valid row(s) have no parseable transaction date (both "
            f"OrdPlaced_Date and Order_InitiatedDate are blank/unusable) -- excluded from the "
            f"Division Trend tab only (still counted in the other two tabs)."
        )

    # A handful of rows can carry a transaction date before the business's actual start (data
    # entry errors, pre-launch test orders) -- left unclipped, these silently create a stray
    # extra period (e.g. an "FY23" point) on the trend chart before the business existed.
    # Clip to the intended window here; still counted in the other two tabs, which don't chart
    # by period.
    start_period = pd.Period(config.TREND_START_MONTH, freq="M")
    end_period = pd.Period(config.TREND_END_MONTH, freq="M")
    txn_period = df["_txn_date"].dt.to_period("M")
    in_window = (txn_period >= start_period) & (txn_period <= end_period)
    out_of_window_rows = int((~in_window).sum())
    if out_of_window_rows:
        db.report_issue(
            "warning",
            f"{out_of_window_rows:,} valid row(s) have a transaction date outside the intended "
            f"Division Trend window ({config.TREND_START_MONTH} to {config.TREND_END_MONTH}) -- "
            f"excluded from the Division Trend tab only (still counted in the other two tabs)."
        )
    df = df[in_window]

    df["_month"] = df["_txn_date"].dt.to_period("M").astype(str)

    group_cols = ["_month", config.SKU_ID_COLUMN, "Item_Description", "Brand", "Division_Name"]
    grouped = (
        df.groupby(group_cols, observed=True)
        .agg(
            amount_sum=("_amount", "sum"),
            invoice_amount_sum=("_invoice_amount", "sum"),
            gross_sales_sum=("_gross_sales", "sum"),
            order_lines=("_amount", "count"),
        )
        .reset_index()
    )

    return [
        {
            "month": r["_month"],
            "item_code": r[config.SKU_ID_COLUMN],
            "item_description": r["Item_Description"],
            "brand": r["Brand"],
            "division": r["Division_Name"],
            "amount_sum": round(float(r["amount_sum"]), 2),
            "invoice_amount_sum": round(float(r["invoice_amount_sum"]), 2),
            "gross_sales_sum": round(float(r["gross_sales_sum"]), 2),
            "order_lines": int(r["order_lines"]),
        }
        for r in grouped.to_dict(orient="records")
    ]


def compute_missing_month_ranges(division_tab):
    """Which months in the tab's intended window (config.TREND_START_MONTH..TREND_END_MONTH)
    have zero data, collapsed into contiguous ranges for a compact UI note."""
    full_range = pd.period_range(config.TREND_START_MONTH, config.TREND_END_MONTH, freq="M")
    months_with_data = {r["month"] for r in division_tab}

    missing = [str(p) for p in full_range if str(p) not in months_with_data]
    if not missing:
        return []

    ranges = []
    start = prev = missing[0]
    for month in missing[1:]:
        if pd.Period(month, freq="M") == pd.Period(prev, freq="M") + 1:
            prev = month
            continue
        ranges.append((start, prev))
        start = prev = month
    ranges.append((start, prev))

    return [start if start == end else f"{start} to {end}" for start, end in ranges]


def resolve_counter_age_cutoff(new_counters_df):
    """Turns config.COUNTER_AGE_CUTOFF_DATE into an actual "YYYY-MM-DD" string (or None) that
    db.join_counter_age() can use directly. "auto" means: use the earliest parseable
    Request_CreatedDate found in the new-counters file itself -- since no row can be earlier
    than the earliest one, this makes every counter in that file "New" without anyone having
    to hand-pick a date."""
    configured = config.COUNTER_AGE_CUTOFF_DATE
    if configured != "auto":
        return configured

    if new_counters_df is None or new_counters_df.empty:
        db.report_issue(
            "warning",
            "COUNTER_AGE_CUTOFF_DATE is 'auto' but the new-counters file wasn't found/loaded "
            "-- every counter will show as 'Old'."
        )
        return None

    parsed_dates = pd.to_datetime(new_counters_df[config.NEW_COUNTERS_DATE_COLUMN], errors="coerce")
    min_date = parsed_dates.min()
    if pd.isna(min_date):
        db.report_issue(
            "warning",
            "COUNTER_AGE_CUTOFF_DATE is 'auto' but no row in the new-counters file has a "
            "parseable Request_CreatedDate -- every counter will show as 'Old'."
        )
        return None

    return min_date.strftime("%Y-%m-%d")


def main():
    run_start = time.perf_counter()
    db.BUILD_ISSUES.clear()

    print("Loading order-line files (data/) ...", flush=True)
    orders_df, load_meta = db.load_order_lines()

    with stage("Loading reference files (new-products, brand master, new-counters)"):
        new_products_df, new_products_meta = db.load_new_products()
        brand_master_df, brand_master_meta = db.load_brand_master()
        new_counters_df, new_counters_meta = db.load_new_counters()

    cutoff_date = resolve_counter_age_cutoff(new_counters_df)

    with stage(f"Computing derived columns for {len(orders_df):,} rows (dates, discount math, buckets)"):
        orders_df = add_derived_columns(orders_df)
        orders_df = db.join_brand_master(orders_df, brand_master_df)
        orders_df = db.join_counter_age(orders_df, new_counters_df, cutoff_date)

        status_counts = orders_df["_status_class"].value_counts().to_dict()
        unclassified_status_display = orders_df.loc[
            orders_df["_status_class"] == "Unclassified", "Order_Status"
        ].apply(lambda s: s if isinstance(s, str) and s.strip() else "(blank)")
        unclassified_status_counts = unclassified_status_display.value_counts(dropna=False).to_dict()
        unclassified_statuses = sorted(unclassified_status_counts.keys())
        # Trace each unrecognized status back to the file it came from. Values like
        # "PTR - Price" or "Revised price = Invoice amt / Qty" are not order statuses at all --
        # they're legend/notes rows that got exported into the data, and knowing WHICH file
        # carries them is the difference between an actionable report and a shrug.
        unclassified_by_file = {}
        unclassified_mask = orders_df["_status_class"] == "Unclassified"
        if unclassified_mask.any():
            for (filename, status), count in (
                orders_df.loc[unclassified_mask]
                .assign(_disp=unclassified_status_display)
                .groupby(["_source_file", "_disp"], observed=True)
                .size().items()
            ):
                unclassified_by_file.setdefault(filename, []).append(
                    {"status": status, "rows": int(count)}
                )
            for entries in unclassified_by_file.values():
                entries.sort(key=lambda e: -e["rows"])
        valid_df = orders_df[orders_df["_status_class"] == "Valid"].copy()

        # Per-file breakdown -- without this, "why is month X missing from Division Trend" or
        # "why are so many rows Unclassified" requires guessing which file is responsible.
        # Grouping by _source_file (added in db.load_order_lines) makes it mechanical instead.
        per_file_summary = []
        for filename, group in orders_df.groupby("_source_file"):
            valid_group = group[group["_status_class"] == "Valid"]
            per_file_summary.append({
                "file": filename,
                "total_rows": int(len(group)),
                "valid_rows": int(len(valid_group)),
                "excluded_rows": int((group["_status_class"] == "Excluded").sum()),
                "unclassified_rows": int((group["_status_class"] == "Unclassified").sum()),
                "valid_rows_undated": int(valid_group["_txn_date"].isna().sum()),
            })
        per_file_summary.sort(key=lambda r: r["file"])

        # Does each file actually contain the months its FILENAME claims? Nothing checked this
        # before, and it is the root cause of the biggest problem in this dataset: a file named
        # "Aug'25 to Oct'25" that really holds Aug-Dec silently duplicates every Nov and Dec
        # order against the dedicated Nov and Dec files. The filename drives the period used for
        # dedup ordering and coverage reporting, so a wrong name quietly corrupts both.
        filename_period_mismatches = []
        for filename, group in orders_df.groupby("_source_file"):
            dated = group["_txn_date"].dropna()
            if dated.empty:
                continue
            try:
                declared_start = pd.Period(group["_period_start"].iloc[0], freq="M")
                declared_end = pd.Period(group["_period_end"].iloc[0], freq="M")
            except (ValueError, TypeError):
                continue
            actual = dated.dt.to_period("M")
            outside = actual[(actual < declared_start) | (actual > declared_end)]
            if len(outside) <= 0.01 * len(dated):
                continue  # a handful of stragglers is normal; a systematic mismatch is not
            extra_months = sorted(str(m) for m in outside.unique())
            filename_period_mismatches.append({
                "file": filename,
                "declared": f"{declared_start}..{declared_end}",
                "actual": f"{actual.min()}..{actual.max()}",
                "rows_outside": int(len(outside)),
                "share_outside": round(100 * len(outside) / len(dated), 1),
                "unexpected_months": extra_months[:12],
            })
        for mm in filename_period_mismatches:
            db.report_issue(
                "error",
                f"FILENAME DOES NOT MATCH CONTENTS: '{mm['file']}' is named for "
                f"{mm['declared']} but actually contains data from {mm['actual']} -- "
                f"{mm['rows_outside']:,} row(s) ({mm['share_outside']}%) fall outside the months "
                f"its name claims, in {mm['unexpected_months']}. If another file also covers "
                f"those months, every order in them is being counted twice.",
                action=f"Confirm what '{mm['file']}' is meant to cover. If the name is wrong, "
                       f"rename it (or add a row to data/file_periods.csv giving its real "
                       f"period_start/period_end). If the extra months are meant to be there, "
                       f"remove the other file(s) covering the same months.",
            )

        # Files whose cancelled/rejected share is wildly off the corpus norm are usually a
        # differently-prepared export -- most often one already pre-filtered to invoiced orders.
        # That is not necessarily wrong, but it means those months aren't directly comparable to
        # the rest, and silently mixing them is exactly how a trend chart lies.
        overall_total = sum(r["total_rows"] for r in per_file_summary)
        overall_excluded = sum(r["excluded_rows"] for r in per_file_summary)
        overall_rate = overall_excluded / overall_total if overall_total else 0.0
        tol = config.EXCLUSION_RATE_ANOMALY_TOLERANCE
        anomalous_files = []
        if overall_rate > 0:
            for r in per_file_summary:
                if r["total_rows"] < 1000:
                    continue  # too small for the rate to mean anything
                rate = r["excluded_rows"] / r["total_rows"]
                if rate < overall_rate * (1 - tol) or rate > overall_rate * (1 + tol):
                    anomalous_files.append({
                        "file": r["file"],
                        "rate": round(100 * rate, 2),
                        "rows": r["total_rows"],
                    })
        if anomalous_files:
            listed = "; ".join(
                f"{a['file']} ({a['rate']}% of {a['rows']:,} rows)" for a in anomalous_files
            )
            db.report_issue(
                "warning",
                f"{len(anomalous_files)} file(s) have a cancelled/rejected rate far from the "
                f"{100 * overall_rate:.1f}% corpus average: {listed}. A rate of 0% usually means "
                f"that export was already filtered to invoiced orders only, so its months carry "
                f"no cancellations while every other month does.",
                action="Confirm with whoever produced those exports whether they were pre-filtered. "
                       "If they were, the months they cover aren't directly comparable to the rest "
                       "on any cancellation-sensitive figure.",
            )

        nan_amount_rows = int(valid_df["_amount"].isna().sum())
        nan_invoice_rows = int(valid_df["_invoice_amount"].isna().sum())
        unmapped_brand_rows = int((valid_df["Brand"] == config.UNMAPPED_BRAND_LABEL).sum())
        new_counter_rows = int((valid_df["Counter_Age"] == "New").sum())

        out_of_range_ptr_rows = int(valid_df["_bucket_ptr_only"].isna().sum())
        out_of_range_total_rows = int(valid_df["_bucket_total"].isna().sum())
        if out_of_range_ptr_rows:
            db.report_issue(
                "warning",
                f"{out_of_range_ptr_rows:,} valid row(s) have a DiscountOnPTR outside 0-100% "
                f"(negative, or the 100.0001% cutoff, likely a data-entry error) -- excluded from "
                f"the Discount Dispersion/NP Discounts tabs' 'DiscountOnPTR only' bucket totals "
                f"(still counted in the Division Trend tab, which doesn't bucket)."
            )
        if out_of_range_total_rows:
            db.report_issue(
                "warning",
                f"{out_of_range_total_rows:,} valid row(s) have a compounded Total Discount "
                f"outside 0-100% -- excluded from the Discount Dispersion/NP Discounts tabs' "
                f"'Total discount' bucket totals (still counted in the Division Trend tab)."
            )

        new_product_skus = db.new_product_sku_set(new_products_df)

        # Match-coverage checks for both reference-file joins -- catches the exact class of
        # problem this project has hit before (wrong file, wrong column, formatting drift):
        # the join runs without error, but silently connects to almost nothing real.
        valid_skus_present = set(valid_df[config.SKU_ID_COLUMN].dropna())
        new_product_skus_matched = len(new_product_skus & valid_skus_present)
        new_product_skus_unmatched = len(new_product_skus) - new_product_skus_matched
        if new_product_skus_unmatched:
            unmatched_sample = sorted(new_product_skus - valid_skus_present)[:5]
            matched_sample = sorted(new_product_skus & valid_skus_present)[:5]
            # Report raw counts, not percentages: 1 match out of 226 renders as "0%", which would
            # read as "nothing matched" right next to advice saying the format is fine.
            if new_product_skus_matched == 0:
                sku_severity, sku_advice = "error", (
                    "NOTHING matched, so the NP Discounts tab is empty and the SKU join is broken. "
                    "Compare an unmatched code above against a real Item_Code from the order data "
                    "-- they are almost certainly formatted differently."
                )
            elif new_product_skus_matched < 0.2 * len(new_product_skus):
                sku_severity, sku_advice = "warning", (
                    f"Only {new_product_skus_matched:,} of {len(new_product_skus):,} matched. That "
                    f"is low enough to be suspicious: check whether the unmatched codes above are "
                    f"formatted like the matched ones. If they are, these products simply haven't "
                    f"sold yet."
                )
            else:
                sku_severity, sku_advice = "warning", (
                    f"{new_product_skus_matched:,} of {len(new_product_skus):,} matched, so the code "
                    f"format is right. The remainder have genuinely not sold yet -- expected for a "
                    f"new-products list, and they will appear as soon as they do."
                )
            db.report_issue(
                sku_severity,
                f"{new_product_skus_unmatched:,}/{len(new_product_skus):,} SKUs on the new-products "
                f"list (item_brand_mapping.csv) have NO matching valid order rows. "
                f"Unmatched examples: {unmatched_sample}. Matched examples: {matched_sample}.",
                action=sku_advice,
            )

        new_counters_registered_total = 0
        new_counters_matched_to_orders = 0
        if new_counters_df is not None and not new_counters_df.empty:
            registered_counter_codes = set(new_counters_df[config.NEW_COUNTERS_ID_COLUMN].dropna())
            valid_counter_codes_present = set(valid_df[config.COUNTER_ID_COLUMN].dropna())
            new_counters_registered_total = len(registered_counter_codes)
            new_counters_matched_to_orders = len(registered_counter_codes & valid_counter_codes_present)
            new_counters_unmatched = new_counters_registered_total - new_counters_matched_to_orders
            if new_counters_unmatched:
                unmatched_c = sorted(registered_counter_codes - valid_counter_codes_present)[:5]
                matched_c = sorted(registered_counter_codes & valid_counter_codes_present)[:5]
                # The right advice depends entirely on whether ANY code matched. If a good share
                # did, the formats obviously agree and the rest simply haven't ordered. If none
                # did, the join is broken and the New/Old split is meaningless -- opposite
                # conclusions, so don't hardcode one of them.
                matched_n = new_counters_matched_to_orders
                total_n = new_counters_registered_total
                if matched_n == 0:
                    severity, advice = "error", (
                        "NOTHING matched, so the New/Old counter split is not working at all and "
                        "every counter is showing as 'Old'. The codes in Allocated_CounterCode "
                        "and Doctor_Code are almost certainly formatted differently -- compare "
                        "the unmatched examples above against a Doctor_Code from the order data."
                    )
                elif matched_n < 0.2 * total_n:
                    severity, advice = "warning", (
                        f"Only {matched_n:,} of {total_n:,} matched. Compare the two example lists "
                        f"-- if they look like different formats, the New/Old split is missing most "
                        f"of its counters. If they look alike, the rest just haven't ordered yet."
                    )
                else:
                    severity, advice = "warning", (
                        f"{matched_n:,} of {total_n:,} matched, so the code formats agree. The rest "
                        f"are most likely counters registered but not yet ordering -- expected, "
                        f"not a bug."
                    )
                db.report_issue(
                    severity,
                    f"{new_counters_unmatched:,}/{new_counters_registered_total:,} counters on the "
                    f"new-counters list (Allocated_CounterCode) have NO matching valid order rows. "
                    f"Unmatched examples: {unmatched_c}. Matched examples: {matched_c}.",
                    action=advice,
                )

    if unclassified_status_counts:
        print(f"  Unclassified Order_Status breakdown ({sum(unclassified_status_counts.values()):,} rows total):", flush=True)
        for status, count in sorted(unclassified_status_counts.items(), key=lambda kv: -kv[1])[:20]:
            print(f"    {count:>10,}  {status!r}", flush=True)
        for filename, entries in sorted(unclassified_by_file.items()):
            shown = ", ".join(f"{e['rows']:,}x {e['status']!r}" for e in entries[:6])
            print(f"      from {filename}: {shown}", flush=True)

        # Some unrecognized values aren't statuses at all -- they're spreadsheet legend/notes
        # text sitting in the Order_Status column, which means those rows are junk rows rather
        # than real orders with an unusual status. Worth calling out separately.
        # Real statuses are plain word phrases ("Order Placed", "Waiting for FF Approval").
        # Legend/notes text gives itself away with arithmetic characters, digits, or a spaced
        # " - " separator. Requiring the hyphen to be spaced avoids flagging a genuine status
        # like "Non-Invoiced".
        def _looks_like_notes(s):
            return (len(s) > 25 or any(c in s for c in "=%/")
                    or any(c.isdigit() for c in s) or " - " in s)

        junk_like = [s for s in unclassified_statuses
                     if s != "(blank)" and _looks_like_notes(s)]
        if junk_like:
            owning_files = sorted(
                f for f, entries in unclassified_by_file.items()
                if any(e["status"] in junk_like for e in entries)
            )
            db.report_issue(
                "warning",
                f"{len(junk_like)} value(s) in the Order_Status column aren't order statuses at "
                f"all -- they look like legend/notes text pasted into the data: {junk_like}. "
                f"Found in: {owning_files}. Those rows were counted as Unclassified and left out "
                f"of every total.",
                action=f"Open {owning_files} and delete the stray notes/legend rows sitting below "
                       f"or beside the real data, then rerun the build.",
            )

    print(f"  Per-file breakdown (total / valid / excluded / unclassified / valid-but-undated):", flush=True)
    name_width = max((len(r["file"]) for r in per_file_summary), default=10)
    for r in per_file_summary:
        flag = "  <-- mostly/all unclassified" if r["total_rows"] and r["unclassified_rows"] / r["total_rows"] > 0.9 else ""
        print(
            f"    {r['file']:<{name_width}}  {r['total_rows']:>10,}  {r['valid_rows']:>10,}  "
            f"{r['excluded_rows']:>10,}  {r['unclassified_rows']:>10,}  {r['valid_rows_undated']:>10,}{flag}",
            flush=True,
        )

    with stage(f"Aggregating {len(valid_df):,} valid rows into the 3 tabs"):
        counter_tab = build_counter_tab(valid_df)
        division_tab = build_division_tab(valid_df)
        np_discounts_tab = build_np_discounts_tab(valid_df, new_product_skus)
        missing_month_ranges = compute_missing_month_ranges(division_tab)

    output = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "row_counts": {
                "total": int(len(orders_df)),
                "valid": int(status_counts.get("Valid", 0)),
                "excluded": int(status_counts.get("Excluded", 0)),
                "unclassified": int(status_counts.get("Unclassified", 0)),
            },
            "unclassified_statuses": unclassified_statuses,
            "unclassified_by_file": unclassified_by_file,
            "exclusion_rate_anomalies": anomalous_files,
            "filename_period_mismatches": filename_period_mismatches,
            "valid_rows_missing_amount": nan_amount_rows,
            "valid_rows_missing_invoice_amount": nan_invoice_rows,
            "valid_rows_with_unmapped_brand": unmapped_brand_rows,
            "files_loaded": load_meta["files_loaded"],
            "files_skipped": load_meta["files_skipped"],
            "duplicate_rows_dropped": load_meta["duplicate_rows_dropped"],
            "duplicate_rows_conflicting": load_meta["duplicate_rows_conflicting"],
            "conflicting_keys_sample": load_meta["conflicting_keys_sample"],
            "conflict_policy": load_meta["conflict_policy"],
            "conflict_extra_rows": load_meta["conflict_extra_rows"],
            "conflict_extra_amount": load_meta["conflict_extra_amount"],
            "conflict_file_pairs": load_meta["conflict_file_pairs"],
            "new_products_files": new_products_meta["new_products_files"],
            "duplicate_new_product_codes": new_products_meta["duplicate_new_product_codes"],
            "new_product_sku_count": len(new_product_skus),
            "new_product_skus_matched": new_product_skus_matched,
            "new_product_skus_unmatched": new_product_skus_unmatched,
            "brand_master_file_present": brand_master_meta["brand_master_file_present"],
            "duplicate_brand_master_codes": brand_master_meta["duplicate_brand_master_codes"],
            "new_counters_file_present": new_counters_meta["new_counters_file_present"],
            "duplicate_counter_codes": new_counters_meta["duplicate_counter_codes"],
            "new_counters_registered_total": new_counters_registered_total,
            "new_counters_matched_to_orders": new_counters_matched_to_orders,
            "counter_age_cutoff_date": cutoff_date,
            "new_counter_valid_rows": new_counter_rows,
            "trend_window": {"start": config.TREND_START_MONTH, "end": config.TREND_END_MONTH},
            "missing_month_ranges": missing_month_ranges,
            "per_file_summary": per_file_summary,
            "build_issues": list(db.BUILD_ISSUES),
        },
        "counter_tab": counter_tab,
        "division_tab": division_tab,
        "np_discounts_tab": np_discounts_tab,
    }

    with stage("Writing output/report_data.json and embedding it into dashboard.html"):
        config.OUTPUT_DIR.mkdir(exist_ok=True)
        report_json_str = json.dumps(output, separators=(",", ":"))
        with open(config.REPORT_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(report_json_str)
        embed_data_in_dashboard(report_json_str)
        dashboard_size_mb = DASHBOARD_HTML_PATH.stat().st_size / 1_000_000 if DASHBOARD_HTML_PATH.exists() else 0

    total_elapsed = time.perf_counter() - run_start
    print(f"\nDone in {total_elapsed:.1f}s total.")
    print(f"Wrote {config.REPORT_JSON_PATH}")
    if DASHBOARD_HTML_PATH.exists():
        print(f"Embedded data into {DASHBOARD_HTML_PATH.name} ({dashboard_size_mb:.1f} MB) -- it now works opened directly (file://), no server needed.")
    else:
        print(f"  {DASHBOARD_HTML_PATH.name} DOES NOT EXIST -- nothing was embedded (see the ERROR above). "
              f"Restore the file (git checkout it, or pull again) and rerun this script.")
    print(f"  rows: {output['meta']['row_counts']}")
    print(f"  counter_tab records: {len(counter_tab):,}")
    print(f"  division_tab records: {len(division_tab):,}")
    print(f"  np_discounts_tab records: {len(np_discounts_tab):,} (from {len(new_product_skus)} new-product SKUs)")
    print(f"    -- {new_product_skus_matched:,}/{len(new_product_skus):,} new-product SKUs matched to real valid order rows")
    print(f"  counter age cutoff: {cutoff_date!r} (config: {config.COUNTER_AGE_CUTOFF_DATE!r}) -- {new_counter_rows:,} valid rows classified 'New'")
    print(f"    -- {new_counters_matched_to_orders:,}/{new_counters_registered_total:,} registered counters matched to real valid order rows")


if __name__ == "__main__":
    main()
