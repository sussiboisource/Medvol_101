"""Independent data-verification tool. Does NOT import build_report_data.py's classification/
date/dedup logic -- everything here is reimplemented separately, on purpose, so this actually
catches bugs in that logic instead of just confirming it agrees with itself.

Two checks:
  A. Row-level arithmetic sanity: for up to N random rows per raw file, does
     PTR * (1-DiscountOnPTR/100) * (1-Cash_Discount/100) * Quantity actually match Amount?
     This is a check on the RAW DATA's internal consistency, independent of any pipeline code.
  B. SKU-level end-to-end reconciliation, per financial year: for up to N random SKUs (pooled
     across all raw files), independently recompute total Amount per FY from the raw files and
     compare against output/report_data.json's division_tab. This verifies the built report
     actually reflects the raw data, catching bugs anywhere in the real pipeline (join, dedup,
     status filtering, aggregation) that a same-code check would miss.

Usage: python verify_data.py [--sample-size 100] [--seed 42]
Requires output/report_data.json to already exist (run build_report_data.py first).
"""

import argparse
import json
import random
import sys

import pandas as pd

import config
import db


MONTH_NUMBERS = db.MONTH_NUMBERS  # reuse the lookup table only, not any classification logic
VALID_STATUSES_NORMALIZED = {" ".join(s.split()).strip().lower() for s in config.VALID_ORDER_STATUSES}


def independent_classify(status):
    """Deliberately re-written from scratch (not imported) so this is a real cross-check
    against build_report_data.classify_status, not a tautology."""
    normalized = " ".join(str(status).split()).strip().lower()
    if normalized in VALID_STATUSES_NORMALIZED:
        return "valid"
    if "reject" in normalized or "cancel" in normalized:
        return "excluded"
    return "unclassified"


def independent_fy(date):
    """FY24 = Apr 2023 - Mar 2024, i.e. FY label = the calendar year the FY *ends* in."""
    if pd.isna(date):
        return None
    fy_end_year = date.year + 1 if date.month >= 4 else date.year
    return f"FY{str(fy_end_year)[-2:]}"


def load_all_raw_rows():
    """Loads every raw order file independently (file discovery/reading reused from db.py --
    that's I/O plumbing, not business logic -- but sanitization, dedup, classification, and
    date handling below are all written fresh for this script)."""
    file_infos, skipped = db.discover_order_files()
    if skipped:
        print(f"NOTE: {len(skipped)} file(s) skipped by file discovery (same as the main build): {skipped}")

    frames = []
    for info in file_infos:
        try:
            raw = db._read_one_file(info["path"])
        except Exception as exc:
            print(f"  Could not read {info['filename']} for verification: {exc}")
            continue
        raw = db.strip_string_columns(raw)
        raw["_source_file"] = info["filename"]
        frames.append(raw)

    if not frames:
        print("No raw files could be loaded. Nothing to verify.")
        sys.exit(1)

    combined = pd.concat(frames, ignore_index=True)

    combined["_status_class"] = combined["Order_Status"].apply(independent_classify)
    combined["_ptr"] = pd.to_numeric(combined["PTR"], errors="coerce").fillna(0.0)
    combined["_qty"] = pd.to_numeric(combined["Quantity"], errors="coerce").fillna(0.0)
    combined["_disc_ptr"] = pd.to_numeric(combined["DiscountOnPTR"], errors="coerce").fillna(0.0)
    combined["_cash_disc"] = pd.to_numeric(combined["Cash_Discount"], errors="coerce").fillna(0.0)
    combined["_amount"] = pd.to_numeric(combined["Amount"], errors="coerce")

    primary = pd.to_datetime(combined[config.PRIMARY_DATE_COLUMN], errors="coerce")
    fallback = pd.to_datetime(combined[config.FALLBACK_DATE_COLUMN], errors="coerce")
    combined["_date"] = primary.fillna(fallback)
    combined["_fy"] = combined["_date"].apply(independent_fy)

    return combined


def dedup_independent(df):
    """Simple from-scratch dedup: same (Item_Code, Order_Number) across DIFFERENT files with
    identical key financial values = same row exported twice, keep one. If values disagree,
    keep both but note it -- this mirrors the main pipeline's policy without calling its code."""
    key_cols = [config.SKU_ID_COLUMN, "Order_Number"]
    value_cols = ["Amount", "InvoiceAmount", "Quantity", "DiscountOnPTR"]
    keep_mask = pd.Series(True, index=df.index)
    conflicts = 0
    for key, group in df.groupby(key_cols):
        if len(group) <= 1 or group["_source_file"].nunique() <= 1:
            continue
        distinct = group[value_cols].drop_duplicates()
        if len(distinct) > 1:
            conflicts += 1
            continue
        keep_mask[group.index[1:]] = False
    return df[keep_mask], conflicts


def check_row_arithmetic(df, sample_size, rng):
    """Part A: independent sanity check on raw rows, no comparison to any built output needed."""
    print(f"\n{'='*70}\nPART A -- Row-level arithmetic sanity check\n{'='*70}")
    for filename, group in df.groupby("_source_file"):
        n = min(sample_size, len(group))
        if n == 0:
            continue
        sample = group.sample(n=n, random_state=rng.randint(0, 2**31))
        fails = []
        for idx, row in sample.iterrows():
            if pd.isna(row["_amount"]) or row["_ptr"] == 0:
                continue  # can't sanity-check rows with no price/amount at all
            expected_price = row["_ptr"] * (1 - row["_disc_ptr"] / 100)
            expected_amount = expected_price * (1 - row["_cash_disc"] / 100) * row["_qty"]
            tolerance = max(1.0, abs(row["_amount"]) * 0.02)
            if abs(expected_amount - row["_amount"]) > tolerance:
                fails.append((row.get("Order_Number"), row.get(config.SKU_ID_COLUMN), row["_amount"], round(expected_amount, 2)))
        status = "OK" if not fails else f"{len(fails)}/{n} MISMATCHES"
        print(f"  {filename}: sampled {n} rows -- {status}")
        for order_num, sku, actual, expected in fails[:10]:
            print(f"      Order {order_num}, SKU {sku}: Amount={actual} but PTR*discounts*Qty={expected}")


def check_sku_reconciliation(df, sample_size, rng):
    """Part B: independent per-FY SKU totals vs the built report_data.json."""
    print(f"\n{'='*70}\nPART B -- SKU-level reconciliation against output/report_data.json, per FY\n{'='*70}")

    if not config.REPORT_JSON_PATH.exists():
        print(f"  {config.REPORT_JSON_PATH} doesn't exist yet -- run build_report_data.py first, then rerun this.")
        return

    with open(config.REPORT_JSON_PATH, encoding="utf-8") as f:
        report = json.load(f)

    valid_df, conflicts = dedup_independent(df[df["_status_class"] == "valid"].copy())
    if conflicts:
        print(f"  NOTE: {conflicts} (SKU, Order_Number) key(s) had conflicting values across files -- excluded from this check.")

    # independent per (SKU, FY) totals from raw data
    independent_totals = (
        valid_df.dropna(subset=["_fy"])
        .groupby([config.SKU_ID_COLUMN, "_fy"])["_amount"]
        .sum()
    )

    # built-report per (SKU, FY) totals, derived from division_tab's month field
    built_totals = {}
    for r in report.get("division_tab", []):
        year, month = r["month"].split("-")
        fy_end_year = int(year) + 1 if int(month) >= 4 else int(year)
        fy = f"FY{str(fy_end_year)[-2:]}"
        built_totals[(r["item_code"], fy)] = built_totals.get((r["item_code"], fy), 0.0) + r["amount_sum"]

    all_skus = valid_df[config.SKU_ID_COLUMN].dropna().unique().tolist()
    n = min(sample_size, len(all_skus))
    sampled_skus = rng.sample(all_skus, n) if n else []

    by_fy = {}
    for sku in sampled_skus:
        sku_fys = independent_totals.loc[sku].index.tolist() if sku in independent_totals.index.get_level_values(0) else []
        for fy in sku_fys:
            expected = float(independent_totals.get((sku, fy), 0.0))
            actual = float(built_totals.get((sku, fy), 0.0))
            tolerance = max(1.0, abs(expected) * 0.02)
            ok = abs(expected - actual) <= tolerance
            by_fy.setdefault(fy, {"checked": 0, "passed": 0, "fails": []})
            by_fy[fy]["checked"] += 1
            by_fy[fy]["passed"] += 1 if ok else 0
            if not ok:
                by_fy[fy]["fails"].append((sku, round(expected, 2), round(actual, 2)))

    if not by_fy:
        print("  No valid SKUs with a resolvable FY were found to sample from.")
        return

    for fy in sorted(by_fy):
        r = by_fy[fy]
        print(f"  {fy}: {r['passed']}/{r['checked']} SKU checks passed")
        for sku, expected, actual in r["fails"][:10]:
            print(f"      SKU {sku}: raw data says {expected}, report_data.json says {actual}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=100, help="Rows/SKUs to sample (default 100)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed, for reproducible sampling (default: random each run)")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    print(f"Verifying with sample size {args.sample_size}" + (f", seed {args.seed}" if args.seed is not None else " (fresh random sample each run)"))

    df = load_all_raw_rows()
    print(f"Loaded {len(df):,} raw rows across {df['_source_file'].nunique()} file(s) for independent verification.")

    check_row_arithmetic(df, args.sample_size, rng)
    check_sku_reconciliation(df, args.sample_size, rng)

    print(f"\n{'='*70}\nDone. This is a sampling check, not exhaustive -- rerun for a different random sample.\n{'='*70}")


if __name__ == "__main__":
    main()
