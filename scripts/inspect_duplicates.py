"""One-off diagnostic: for a sample of (Item_Code, Order_Number) keys that appear in more
than one file with different financial values, print every row involved in full detail --
source file, counter, dates, status, and the values themselves -- so it's possible to tell
by eye whether this is the same real order re-exported later with updated values, or two
genuinely different orders that happen to share an Order_Number.

Not part of the main pipeline or verify_data.py -- this is purely for looking at the
111k+ "conflicting" rows flagged by db.load_order_lines() and deciding what they actually are.

Usage: python inspect_duplicates.py [--sample-size 15] [--seed 42]
"""

import argparse
import random

import pandas as pd

import config
import db

DETAIL_COLS = [
    "_source_file", config.COUNTER_ID_COLUMN, "Doctor_Name", config.PRIMARY_DATE_COLUMN,
    config.FALLBACK_DATE_COLUMN, "Order_Status", "Amount", "InvoiceAmount", "Quantity",
    "DiscountOnPTR", "Cash_Discount", "PTR",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=15, help="Conflicting keys to show (default 15)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible sampling")
    args = parser.parse_args()
    rng = random.Random(args.seed)

    file_infos, skipped = db.discover_order_files()
    if skipped:
        print(f"NOTE: {len(skipped)} file(s) skipped by discovery: {skipped}")

    frames = []
    for info in file_infos:
        try:
            raw = db._read_one_file(info["path"])
        except Exception as exc:
            print(f"  Could not read {info['filename']}: {exc}")
            continue
        raw = db.strip_string_columns(raw)
        raw["_source_file"] = info["filename"]
        frames.append(raw)

    if not frames:
        print("No files could be loaded.")
        return

    combined = pd.concat(frames, ignore_index=True)
    dup_key = [config.SKU_ID_COLUMN, "Order_Number"]
    value_check_cols = ["Amount", "InvoiceAmount", "Quantity", "DiscountOnPTR"]

    conflicting_groups = []
    for key, group in combined.groupby(dup_key):
        if len(group) <= 1 or group["_source_file"].nunique() <= 1:
            continue
        distinct_value_rows = group[value_check_cols].drop_duplicates()
        if len(distinct_value_rows) > 1:
            conflicting_groups.append((key, group))

    print(f"Total conflicting (Item_Code, Order_Number) keys found: {len(conflicting_groups):,}\n")
    if not conflicting_groups:
        return

    n = min(args.sample_size, len(conflicting_groups))
    sample = rng.sample(conflicting_groups, n)

    for key, group in sample:
        sku, order_num = key
        print(f"{'='*90}")
        print(f"SKU {sku}, Order_Number {order_num} -- {len(group):,} row(s) across "
              f"{group['_source_file'].nunique()} file(s)")
        print(f"{'='*90}")
        cols = [c for c in DETAIL_COLS if c in group.columns]
        print(group[cols].to_string(index=False))
        print()


if __name__ == "__main__":
    main()
