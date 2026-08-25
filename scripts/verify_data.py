"""Independent data-verification tool. Does NOT import build_report_data.py's classification/
date/dedup logic -- everything here is reimplemented separately, on purpose, so this actually
catches bugs in that logic instead of just confirming it agrees with itself.

Six checks, each independent of the others -- one crashing does NOT stop the rest, and
whatever ran (plus the crash itself, if any) always gets written to
output/verification_report.txt so there's a single, complete, pasteable file to hand back for
diagnosis instead of copying partial/stale terminal scrollback:
  0. File health check: for EVERY file discovered in data/ (every raw order file, whether it has
     0 rows or a million, plus the three reference files) -- confirms it actually opens, reports
     its row count, and flags any missing/unexpected columns. This is the only check that's
     guaranteed to mention every single file by name; Parts A/B only ever look at files that
     produced at least one sampled row, so a file that's empty, unreadable, or silently missing
     from disk would otherwise never show up anywhere in this report.
  D. Period coverage: purely from the discovered files' own declared periods (not the built
     report) -- are there any months in the intended trend window not covered by any file, and
     do any files declare overlapping periods (informational -- expected for real re-exports,
     but worth seeing).
  E. Header consistency: do all discovered order files agree on the columns the pipeline
     actually reads (IMPORTANT_COLUMNS -- dates, discount math, status, join keys)? Part 0
     already flags a file individually missing from the full 57-column expected list, but
     that's one line inside a long per-file row; this is a focused, direct file-by-file
     comparison on just the columns that matter for correctness.
  A. Row-level arithmetic sanity: for up to N random rows per raw file, does
     PTR * (1-DiscountOnPTR/100) * (1-Cash_Discount/100) * Quantity actually match Amount?
     (Skips rows where FixedPrice overrides that formula.) Independent of any pipeline code.
  B. SKU-level end-to-end reconciliation, per financial year: for up to N random SKUs (pooled
     across all raw files), independently recompute total Amount per FY from the raw files and
     compare against output/report_data.json's division_tab. Catches bugs anywhere in the real
     pipeline (join, dedup, status filtering, aggregation) that a same-code check would miss.
  C. Build-time issues: pulls report_data.json's own meta.build_issues (files that failed to
     read, severe schema mismatches, etc. -- see db.py's report_issue) into this same report,
     so build-time and verification-time findings live in one place.

A final SUMMARY block (counts only, backed by the detail in the parts above) prints at the end,
so "what's actually wrong with data/ right now" is answerable in one glance instead of reading
through five sections of detail.

Usage: python verify_data.py [--sample-size 100] [--seed 42]
Requires output/report_data.json to already exist (run build_report_data.py first).
"""

import argparse
import json
import random
import sys
import time
import traceback
from contextlib import contextmanager

import pandas as pd

import config
import db


VALID_STATUSES_NORMALIZED = {" ".join(s.split()).strip().lower() for s in config.VALID_ORDER_STATUSES}
REPORT_LINES = []

# The columns the pipeline actually reads -- dates, discount math, status classification,
# join keys. Not the full 57-column EXPECTED_COLUMNS list (most of which, e.g. HQ or
# Position_Code, are never touched by any logic) -- just the ones where a missing/renamed
# column would silently break something.
IMPORTANT_COLUMNS = [
    "Order_Number", config.PRIMARY_DATE_COLUMN, config.FALLBACK_DATE_COLUMN,
    config.SKU_ID_COLUMN, "Item_Description", "Division_Name", "Quantity",
    "Amount", "InvoiceAmount", "Order_Status", "PTR", "DiscountOnPTR",
    "Cash_Discount", "FixedPrice", config.COUNTER_ID_COLUMN,
]

# Tallied as each check runs, printed as one scannable block at the very end -- so "what's wrong
# with data/ right now" has a single answer instead of requiring a read-through of every part.
SUMMARY_COUNTS = {
    "files_failed_to_open": 0,
    "files_skipped_unparseable_name": 0,
    "files_with_column_issues": 0,
    "period_coverage_gap_months": 0,
    "header_mismatch_files": 0,
    "arithmetic_mismatches": 0,
    "reconciliation_mismatches": 0,
    "build_time_errors": 0,
    "build_time_warnings": 0,
}


def log(message=""):
    """Prints AND appends to the report buffer -- every line the console shows also ends up
    in the saved report file, so nothing said "while it was running" gets lost."""
    print(message, flush=True)
    REPORT_LINES.append(message)


TOTAL_STAGES = 7


@contextmanager
def stage(label, step):
    """Overall run progress -- '[step/TOTAL_STAGES] label ... done in Xs', logged into the
    saved report too (not just the console) so a slow real-scale run's timing is visible after
    the fact, not just while watching the terminal."""
    log(f"\n[{step}/{TOTAL_STAGES}] {label} ...")
    t0 = time.perf_counter()
    yield
    log(f"[{step}/{TOTAL_STAGES}] done in {time.perf_counter() - t0:.1f}s")


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


def check_file_health():
    """Part 0: confirms every file discovered in data/ actually opens, reports its row count,
    and flags missing/unexpected columns. Deliberately covers ALL of them, including 0-row
    placeholders and files that fail to open -- Parts A/B only ever see files that produced at
    least one sampled row, so a file that's empty, corrupt, or silently absent from disk would
    otherwise never be mentioned anywhere in this report."""
    log(f"\n{'='*70}\nPART 0 -- File health check (every file discovered in data/)\n{'='*70}")

    file_infos, skipped = db.discover_order_files()
    if skipped:
        SUMMARY_COUNTS["files_skipped_unparseable_name"] += len(skipped)
        log(f"  {len(skipped)} file(s) in data/ have NO parseable period in the filename and no "
            f"file_periods.csv entry -- never even opened: {skipped}")

    expected_cols = set(config.EXPECTED_COLUMNS)
    total_rows = 0
    opened_ok = 0
    total_files = len(file_infos)
    log(f"\n  Order-line files ({total_files} discovered):")
    for i, info in enumerate(file_infos, start=1):
        print(f"  {db.progress_bar(i - 1, total_files)} opening {info['filename']} ...", flush=True)
        try:
            raw = db._read_one_file(info["path"])
        except Exception as exc:
            SUMMARY_COUNTS["files_failed_to_open"] += 1
            log(f"    {info['filename']:<55} FAILED to open -- {type(exc).__name__}: {exc}")
            continue
        opened_ok += 1
        total_rows += len(raw)
        missing = expected_cols - set(raw.columns)
        if not missing:
            note = ""
        elif len(missing) > len(expected_cols) / 2:
            SUMMARY_COUNTS["files_with_column_issues"] += 1
            shown = list(raw.columns)[:8]
            note = f"  !! MOST/ALL expected columns missing (found instead: {shown}{'...' if len(raw.columns) > 8 else ''})"
        else:
            SUMMARY_COUNTS["files_with_column_issues"] += 1
            note = f"  missing columns: {sorted(missing)}"
        log(f"    {info['filename']:<55} OK   {len(raw):>10,} rows  (period {info['period_start']}-{info['period_end']}){note}")

    log(f"\n  {opened_ok}/{len(file_infos)} order-line file(s) opened successfully, "
        f"{total_rows:,} rows combined (before dedup), {len(file_infos) - opened_ok} failed to open.")

    log(f"\n  Reference files:")
    reference_checks = [
        (
            "new-products list",
            sorted(
                p for p in config.DATA_DIR.glob(f"{config.NEW_PRODUCTS_FILE_PREFIX}*")
                if p.suffix.lower() in (".csv", ".xlsx")
            ) if config.DATA_DIR.exists() else [],
            [config.NEW_PRODUCTS_SKU_COLUMN],
        ),
        (
            "brand master",
            [config.BRAND_MASTER_FILE] if config.BRAND_MASTER_FILE.exists() else [],
            [config.BRAND_MASTER_SKU_COLUMN, config.BRAND_MASTER_BRAND_COLUMN],
        ),
        (
            "new-counters log",
            [config.NEW_COUNTERS_FILE] if config.NEW_COUNTERS_FILE.exists() else [],
            [config.NEW_COUNTERS_ID_COLUMN, config.NEW_COUNTERS_DATE_COLUMN],
        ),
    ]
    for label, paths, expected in reference_checks:
        if not paths:
            log(f"    {label:<20} NOT PRESENT (falls back to the documented default)")
            continue
        for p in paths:
            try:
                raw = db._read_one_file(p)
            except Exception as exc:
                SUMMARY_COUNTS["files_failed_to_open"] += 1
                log(f"    {label:<20} {p.name:<45} FAILED to open -- {type(exc).__name__}: {exc}")
                continue
            missing = set(expected) - set(raw.columns)
            if missing:
                SUMMARY_COUNTS["files_with_column_issues"] += 1
            note = f"  missing columns: {sorted(missing)}" if missing else ""
            log(f"    {label:<20} {p.name:<45} OK   {len(raw):>8,} rows{note}")


def _collapse_month_ranges(months):
    """['2024-10', '2024-11', '2024-12', '2025-04'] -> ['2024-10 to 2024-12', '2025-04']."""
    if not months:
        return []
    ranges = []
    start = prev = months[0]
    for m in months[1:]:
        if pd.Period(m, freq="M") == pd.Period(prev, freq="M") + 1:
            prev = m
            continue
        ranges.append(start if start == prev else f"{start} to {prev}")
        start = prev = m
    ranges.append(start if start == prev else f"{start} to {prev}")
    return ranges


def check_period_coverage():
    """Part D: purely from the files discovered in data/ and their own declared periods (NOT
    the built report_data.json / division_tab) -- are there months in the intended trend window
    that no file covers at all, and do any files declare overlapping periods. This is a
    folder-level check: it runs even if every file is a 0-row placeholder, since it's about
    which periods data/ *claims* to cover, not what's actually inside the files yet."""
    log(f"\n{'='*70}\nPART D -- Data folder period coverage (from filenames, not row data)\n{'='*70}")

    file_infos, _ = db.discover_order_files()
    if not file_infos:
        log("  No order files discovered -- nothing to check.")
        return

    covered = set()
    for info in file_infos:
        for p in pd.period_range(info["period_start"], info["period_end"], freq="M"):
            covered.add(str(p))

    full_range = pd.period_range(config.TREND_START_MONTH, config.TREND_END_MONTH, freq="M")
    missing = [str(p) for p in full_range if str(p) not in covered]
    SUMMARY_COUNTS["period_coverage_gap_months"] = len(missing)
    if missing:
        log(f"  {len(missing)} month(s) in the intended window ({config.TREND_START_MONTH} to "
            f"{config.TREND_END_MONTH}) are NOT covered by any file's declared period: "
            f"{', '.join(_collapse_month_ranges(missing))}")
    else:
        log(f"  No gaps: every month from {config.TREND_START_MONTH} to {config.TREND_END_MONTH} "
            f"is covered by at least one file's declared period.")

    overlaps = []
    for i, a in enumerate(file_infos):
        a_months = {str(p) for p in pd.period_range(a["period_start"], a["period_end"], freq="M")}
        for b in file_infos[i + 1:]:
            b_months = {str(p) for p in pd.period_range(b["period_start"], b["period_end"], freq="M")}
            shared = sorted(a_months & b_months)
            if shared:
                overlaps.append((a["filename"], b["filename"], shared))
    if overlaps:
        log(f"\n  {len(overlaps)} file pair(s) declare overlapping periods (not necessarily a "
            f"problem -- real re-exports covering the same months are expected; identical rows "
            f"get deduped automatically, conflicting values are flagged in Part C):")
        for a, b, months in overlaps[:15]:
            span = months[0] if len(months) == 1 else f"{months[0]} to {months[-1]}"
            log(f"    '{a}'  <->  '{b}'   ({span})")


def check_header_consistency():
    """Part E: do all discovered order files agree on the columns the pipeline actually
    reads (IMPORTANT_COLUMNS)? Opens every file fresh (independent of Part 0's own read) and
    checks each one against the same fixed list, so the answer to "do all 15 files have
    matching headers" is one direct table instead of something you'd have to piece together
    from Part 0's per-file missing-column notes."""
    log(f"\n{'='*70}\nPART E -- Header consistency across order files (important columns only)\n{'='*70}")
    log(f"  Checking {len(IMPORTANT_COLUMNS)} column(s): {', '.join(IMPORTANT_COLUMNS)}")

    file_infos, _ = db.discover_order_files()
    if not file_infos:
        log("  No order files discovered -- nothing to check.")
        return

    per_file_missing = {}
    for info in file_infos:
        try:
            raw = db._read_one_file(info["path"])
        except Exception as exc:
            log(f"    {info['filename']:<55} FAILED to open -- {type(exc).__name__}: {exc}")
            continue
        per_file_missing[info["filename"]] = [c for c in IMPORTANT_COLUMNS if c not in raw.columns]

    if not per_file_missing:
        return

    clean_count = sum(1 for missing in per_file_missing.values() if not missing)
    log(f"\n  {clean_count}/{len(per_file_missing)} file(s) have all {len(IMPORTANT_COLUMNS)} "
        f"important columns present with matching names.")

    name_width = max(len(f) for f in per_file_missing)
    for filename in sorted(per_file_missing):
        missing = per_file_missing[filename]
        if missing:
            SUMMARY_COUNTS["header_mismatch_files"] += 1
            log(f"    {filename:<{name_width}}  MISSING: {missing}")
        else:
            log(f"    {filename:<{name_width}}  OK")


def load_all_raw_rows():
    """Loads every raw order file independently (file discovery/reading reused from db.py --
    that's I/O plumbing, not business logic -- but sanitization, dedup, classification, and
    date handling below are all written fresh for this script)."""
    file_infos, skipped = db.discover_order_files()
    if skipped:
        log(f"NOTE: {len(skipped)} file(s) skipped by file discovery (same as the main build): {skipped}")

    frames = []
    total_files = len(file_infos)
    for i, info in enumerate(file_infos, start=1):
        print(f"  {db.progress_bar(i - 1, total_files)} loading {info['filename']} ...", flush=True)
        try:
            raw = db._read_one_file(info["path"])
        except Exception as exc:
            log(f"  Could not read {info['filename']} for verification: {exc}")
            continue
        raw = db.strip_string_columns(raw)
        raw["_source_file"] = info["filename"]
        frames.append(raw)

    print(f"  {db.progress_bar(total_files, total_files)} loaded {len(frames)}/{total_files} file(s)", flush=True)

    if not frames:
        log("No raw files could be loaded. Nothing to verify.")
        return None

    combined = pd.concat(frames, ignore_index=True)

    # db.parse_dates handles the .xlsb-via-pyxlsb quirk (raw Excel serial numbers instead of
    # real dates) -- this is I/O-level parsing, same category as file reading, not the
    # business logic this module is meant to reimplement independently.
    primary = db.parse_dates(combined[config.PRIMARY_DATE_COLUMN])
    fallback = db.parse_dates(combined[config.FALLBACK_DATE_COLUMN])
    date = primary.fillna(fallback)

    # Built as one dict + a single pd.concat, not repeated combined["_x"] = ... assignments --
    # the latter fragments the DataFrame badly at 1M+ rows (pandas PerformanceWarning).
    derived = pd.DataFrame({
        "_status_class": combined["Order_Status"].apply(independent_classify),
        "_ptr": pd.to_numeric(combined["PTR"], errors="coerce").fillna(0.0),
        "_qty": pd.to_numeric(combined["Quantity"], errors="coerce").fillna(0.0),
        "_disc_ptr": pd.to_numeric(combined["DiscountOnPTR"], errors="coerce").fillna(0.0),
        "_cash_disc": pd.to_numeric(combined["Cash_Discount"], errors="coerce").fillna(0.0),
        "_fixed_price": pd.to_numeric(combined["FixedPrice"], errors="coerce").fillna(0.0),
        "_amount": pd.to_numeric(combined["Amount"], errors="coerce"),
        "_date": date,
    }, index=combined.index)
    derived["_fy"] = derived["_date"].apply(independent_fy)

    return pd.concat([combined, derived], axis=1)


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
    log(f"\n{'='*70}\nPART A -- Row-level arithmetic sanity check\n{'='*70}")
    for filename, group in df.groupby("_source_file"):
        n = min(sample_size, len(group))
        if n == 0:
            continue
        sample = group.sample(n=n, random_state=rng.randint(0, 2**31))
        fails = []
        skipped_fixed_price = 0
        for idx, row in sample.iterrows():
            if pd.isna(row["_amount"]) or row["_ptr"] == 0:
                continue  # can't sanity-check rows with no price/amount at all
            if row["_fixed_price"] != 0:
                skipped_fixed_price += 1
                continue  # FixedPrice overrides the PTR-discount formula for this row
            expected_price = row["_ptr"] * (1 - row["_disc_ptr"] / 100)
            expected_amount = expected_price * (1 - row["_cash_disc"] / 100) * row["_qty"]
            tolerance = max(1.0, abs(row["_amount"]) * 0.02)
            if abs(expected_amount - row["_amount"]) > tolerance:
                fails.append((row.get("Order_Number"), row.get(config.SKU_ID_COLUMN), row["_amount"], round(expected_amount, 2)))
        SUMMARY_COUNTS["arithmetic_mismatches"] += len(fails)
        status = "OK" if not fails else f"{len(fails)}/{n} MISMATCHES"
        note = f" ({skipped_fixed_price} FixedPrice row(s) skipped, formula doesn't apply)" if skipped_fixed_price else ""
        log(f"  {filename}: sampled {n} rows -- {status}{note}")
        for order_num, sku, actual, expected in fails[:10]:
            log(f"      Order {order_num}, SKU {sku}: Amount={actual} but PTR*discounts*Qty={expected}")


def check_sku_reconciliation(df, sample_size, rng):
    """Part B: independent per-FY SKU totals vs the built report_data.json."""
    log(f"\n{'='*70}\nPART B -- SKU-level reconciliation against output/report_data.json, per FY\n{'='*70}")

    if not config.REPORT_JSON_PATH.exists():
        log(f"  {config.REPORT_JSON_PATH} doesn't exist yet -- run build_report_data.py first, then rerun this.")
        return

    with open(config.REPORT_JSON_PATH, encoding="utf-8") as f:
        report = json.load(f)

    valid_df, conflicts = dedup_independent(df[df["_status_class"] == "valid"].copy())
    if conflicts:
        log(f"  NOTE: {conflicts} (SKU, Order_Number) key(s) had conflicting values across files -- excluded from this check.")

    # independent per (SKU, FY) totals from raw data
    independent_totals = (
        valid_df.dropna(subset=["_fy"])
        .groupby([config.SKU_ID_COLUMN, "_fy"])["_amount"]
        .sum()
    )

    # built-report per (SKU, FY) totals, derived from division_tab's month field
    built_totals = {}
    bad_month_rows = 0
    for r in report.get("division_tab", []):
        parts = r["month"].split("-")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            bad_month_rows += 1  # shouldn't happen after the build_division_tab fix, but don't crash if it does
            continue
        year, month = parts
        fy_end_year = int(year) + 1 if int(month) >= 4 else int(year)
        fy = f"FY{str(fy_end_year)[-2:]}"
        built_totals[(r["item_code"], fy)] = built_totals.get((r["item_code"], fy), 0.0) + r["amount_sum"]
    if bad_month_rows:
        log(f"  NOTE: {bad_month_rows} division_tab record(s) had an unparseable 'month' value and were skipped in this comparison.")

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
        log("  No valid SKUs with a resolvable FY were found to sample from.")
        return

    for fy in sorted(by_fy):
        r = by_fy[fy]
        SUMMARY_COUNTS["reconciliation_mismatches"] += len(r["fails"])
        log(f"  {fy}: {r['passed']}/{r['checked']} SKU checks passed")
        for sku, expected, actual in r["fails"][:10]:
            log(f"      SKU {sku}: raw data says {expected}, report_data.json says {actual}")


def check_build_issues():
    """Part C: pulls report_data.json's own build_issues (from db.report_issue calls during
    build_report_data.py) into this same report, so a copy-paste of ONE file covers both
    "did the build hit problems" and "did independent verification find problems"."""
    log(f"\n{'='*70}\nPART C -- Issues reported by the build itself (output/report_data.json meta)\n{'='*70}")

    if not config.REPORT_JSON_PATH.exists():
        log(f"  {config.REPORT_JSON_PATH} doesn't exist yet -- run build_report_data.py first, then rerun this.")
        return

    with open(config.REPORT_JSON_PATH, encoding="utf-8") as f:
        report = json.load(f)

    issues = report.get("meta", {}).get("build_issues", [])
    if not issues:
        log("  None recorded.")
        return
    for issue in issues:
        severity = issue.get("severity", "?")
        if severity == "error":
            SUMMARY_COUNTS["build_time_errors"] += 1
        elif severity == "warning":
            SUMMARY_COUNTS["build_time_warnings"] += 1
        log(f"  [{severity.upper()}] {issue.get('message', issue)}")


def run_check(label, fn, *args):
    """Runs one check; if it throws, the failure (with full traceback) goes into the report
    instead of killing the process and losing everything else that would have run."""
    try:
        fn(*args)
    except Exception:
        log(f"\n{'!'*70}\n{label} CRASHED -- see traceback below. Other checks still ran/will run.\n{'!'*70}")
        log(traceback.format_exc())


SUMMARY_LABELS = {
    "files_failed_to_open": "Files that failed to open (Part 0)",
    "files_skipped_unparseable_name": "Files skipped -- unparseable filename (Part 0)",
    "files_with_column_issues": "Files with missing/unexpected columns (Part 0)",
    "period_coverage_gap_months": "Months with no file covering them (Part D)",
    "header_mismatch_files": "Files missing an important column (Part E)",
    "arithmetic_mismatches": "Row arithmetic mismatches (Part A)",
    "reconciliation_mismatches": "SKU-level reconciliation mismatches (Part B)",
    "build_time_errors": "Build-time errors -- data likely missing (Part C)",
    "build_time_warnings": "Build-time warnings (Part C)",
}


def print_summary():
    """The single 'so what's actually wrong with data/ right now' answer -- every other part
    is detail backing up one of these numbers. Read this first; go to the matching PART above
    only for the ones that aren't zero."""
    log(f"\n{'='*70}\nSUMMARY -- issues found (see the matching PART above for detail)\n{'='*70}")
    total = sum(SUMMARY_COUNTS.values())
    width = max(len(v) for v in SUMMARY_LABELS.values())
    for key, label in SUMMARY_LABELS.items():
        count = SUMMARY_COUNTS[key]
        flag = "  <-- " if count else ""
        log(f"  {label:<{width}} : {count:>6,}{flag}")
    log("-" * 70)
    if total == 0:
        log("  CLEAN -- no issues found by any check in this run.")
    else:
        log(f"  {total:,} total issue(s) found across all checks -- see the PARTs flagged above.")


def write_report_file():
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    report_path = config.OUTPUT_DIR / "verification_report.txt"
    report_path.write_text("\n".join(REPORT_LINES), encoding="utf-8")
    print(f"\nFull report written to {report_path} -- paste this file's contents back if anything here needs diagnosing.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=100, help="Rows/SKUs to sample (default 100)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed, for reproducible sampling (default: random each run)")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    log(f"Verifying with sample size {args.sample_size}" + (f", seed {args.seed}" if args.seed is not None else " (fresh random sample each run)"))

    with stage("PART 0 -- file health check", 1):
        run_check("PART 0", check_file_health)

    with stage("PART D -- period coverage", 2):
        run_check("PART D", check_period_coverage)

    with stage("PART E -- header consistency", 3):
        run_check("PART E", check_header_consistency)

    with stage("Loading raw rows for Parts A/B", 4):
        try:
            df = load_all_raw_rows()
        except Exception:
            log(f"\n{'!'*70}\nLoading raw files CRASHED -- see traceback below.\n{'!'*70}")
            log(traceback.format_exc())
            df = None

    with stage("PART A -- row arithmetic sanity", 5):
        if df is not None:
            run_check("PART A", check_row_arithmetic, df, args.sample_size, rng)
        else:
            log("  Skipping -- no raw data could be loaded.")

    with stage("PART B -- SKU reconciliation", 6):
        if df is not None:
            run_check("PART B", check_sku_reconciliation, df, args.sample_size, rng)
        else:
            log("  Skipping -- no raw data could be loaded.")

    with stage("PART C -- build-time issues", 7):
        run_check("PART C", check_build_issues)

    print_summary()

    log(f"\n{'='*70}\nDone. This is a sampling check, not exhaustive -- rerun for a different random sample.\n{'='*70}")
    write_report_file()


if __name__ == "__main__":
    main()
