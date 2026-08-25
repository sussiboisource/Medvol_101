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
        return
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    safe_json = report_json_str.replace("</script>", "<\\/script>")
    new_html, count = REPORT_DATA_SCRIPT_RE.subn(
        lambda m: m.group(1) + safe_json + m.group(3), html
    )
    if count == 0:
        print("WARNING: could not find the report-data <script> tag in dashboard.html -- "
              "the embedded copy was not updated. dashboard.html will still work if served "
              "over HTTP (output/report_data.json is current).")
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
    primary = pd.to_datetime(df[config.PRIMARY_DATE_COLUMN], errors="coerce")
    fallback = pd.to_datetime(df[config.FALLBACK_DATE_COLUMN], errors="coerce")
    return primary.fillna(fallback)


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
    return df


def add_derived_columns(df):
    df = df.copy()
    df = canonicalize_labels(df)

    df["_status_class"] = df["Order_Status"].apply(classify_status)
    df["_txn_date"] = compute_canonical_date(df)

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
    """Grain: Counter_Age x SKU x discount bucket. Counter_Age is pre-computed once at build
    time (config.COUNTER_AGE_CUTOFF_DATE) -- per-counter (Doctor_Code) granularity was tried
    and abandoned: at real data scale it produced 300k+ JSON records and made the dashboard
    unusable. Only 2 age values instead of thousands of counters keeps this small."""
    # Brand is deliberately excluded here -- only the Division Trend tab needs it. Fewer
    # group-by columns also means fewer distinct records, which matters at real data scale.
    records = []
    group_cols = ["Counter_Age", config.SKU_ID_COLUMN, "Item_Description", "Division_Name"]

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
    group_cols = [config.SKU_ID_COLUMN, "Item_Description", "Division_Name"]

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
    df = valid_df.copy()
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


def main():
    run_start = time.perf_counter()
    db.BUILD_ISSUES.clear()

    print("Loading order-line files (data/) ...", flush=True)
    orders_df, load_meta = db.load_order_lines()

    with stage("Loading reference files (new-products, brand master, new-counters)"):
        new_products_df, new_products_meta = db.load_new_products()
        brand_master_df, brand_master_meta = db.load_brand_master()
        new_counters_df, new_counters_meta = db.load_new_counters()

    with stage(f"Computing derived columns for {len(orders_df):,} rows (dates, discount math, buckets)"):
        orders_df = add_derived_columns(orders_df)
        orders_df = db.join_brand_master(orders_df, brand_master_df)
        orders_df = db.join_counter_age(orders_df, new_counters_df, config.COUNTER_AGE_CUTOFF_DATE)

        status_counts = orders_df["_status_class"].value_counts().to_dict()
        unclassified_status_counts = (
            orders_df.loc[orders_df["_status_class"] == "Unclassified", "Order_Status"]
            .value_counts().to_dict()
        )
        unclassified_statuses = sorted(unclassified_status_counts.keys())
        valid_df = orders_df[orders_df["_status_class"] == "Valid"].copy()

        nan_amount_rows = int(valid_df["_amount"].isna().sum())
        nan_invoice_rows = int(valid_df["_invoice_amount"].isna().sum())
        unmapped_brand_rows = int((valid_df["Brand"] == config.UNMAPPED_BRAND_LABEL).sum())
        new_counter_rows = int((valid_df["Counter_Age"] == "New").sum())

        new_product_skus = db.new_product_sku_set(new_products_df)

    if unclassified_status_counts:
        print(f"  Unclassified Order_Status breakdown ({sum(unclassified_status_counts.values()):,} rows total):", flush=True)
        for status, count in sorted(unclassified_status_counts.items(), key=lambda kv: -kv[1])[:20]:
            print(f"    {count:>10,}  {status!r}", flush=True)

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
            "valid_rows_missing_amount": nan_amount_rows,
            "valid_rows_missing_invoice_amount": nan_invoice_rows,
            "valid_rows_with_unmapped_brand": unmapped_brand_rows,
            "files_loaded": load_meta["files_loaded"],
            "files_skipped": load_meta["files_skipped"],
            "duplicate_rows_dropped": load_meta["duplicate_rows_dropped"],
            "duplicate_rows_conflicting": load_meta["duplicate_rows_conflicting"],
            "conflicting_keys_sample": load_meta["conflicting_keys_sample"],
            "new_products_files": new_products_meta["new_products_files"],
            "duplicate_new_product_codes": new_products_meta["duplicate_new_product_codes"],
            "new_product_sku_count": len(new_product_skus),
            "brand_master_file_present": brand_master_meta["brand_master_file_present"],
            "duplicate_brand_master_codes": brand_master_meta["duplicate_brand_master_codes"],
            "new_counters_file_present": new_counters_meta["new_counters_file_present"],
            "duplicate_counter_codes": new_counters_meta["duplicate_counter_codes"],
            "counter_age_cutoff_date": config.COUNTER_AGE_CUTOFF_DATE,
            "new_counter_valid_rows": new_counter_rows,
            "trend_window": {"start": config.TREND_START_MONTH, "end": config.TREND_END_MONTH},
            "missing_month_ranges": missing_month_ranges,
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
    print(f"Embedded data into {DASHBOARD_HTML_PATH.name} ({dashboard_size_mb:.1f} MB) -- it now works opened directly (file://), no server needed.")
    print(f"  rows: {output['meta']['row_counts']}")
    print(f"  counter_tab records: {len(counter_tab):,}")
    print(f"  division_tab records: {len(division_tab):,}")
    print(f"  np_discounts_tab records: {len(np_discounts_tab):,} (from {len(new_product_skus)} new-product SKUs)")
    print(f"  counter age cutoff: {config.COUNTER_AGE_CUTOFF_DATE!r} -- {new_counter_rows:,} valid rows classified 'New'")


if __name__ == "__main__":
    main()
