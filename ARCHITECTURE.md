# Project Architecture — Medvol Discount/Sales Dashboard

What every file and folder in this project is for. Updated as the project grows — if a file
exists on disk but isn't listed here, or is listed here but doesn't exist yet, this doc is stale
and should be fixed.

## Why "folder inside folder"

Each folder has exactly one job, so you always know where to look:
- **`data/`** — raw inputs. Nothing in this project ever rewrites a file in here once it lands.
- **`scripts/`** — code that reads `data/` and does the thinking.
- **`output/`** — small, generated results the dashboard actually reads. Safe to delete and regenerate anytime by rerunning the scripts.
- Project root — the dashboard itself and docs like this one.

If a change is needed, this separation tells you which folder to touch: new data → `data/`,
new business logic → `scripts/`, nothing in `output/` or the dashboard should ever be hand-edited,
since both are always generated fresh from the layers below them.

## File-by-file

### `data/` — raw, untouched inputs

| File | Status | What it is |
|------|--------|------------|
| `SCHEMA.md` | ✅ exists (some sections stale) | Data dictionary: every column, what it means, the confirmed product hierarchy (Vertical → Division → Brand), and the three row-classification/date rules. Written early against a 98-row sample — the "not yet written" references to `db.py`/`build_report_data.py` are stale (both exist and are described below), but the column meanings and formulas are still accurate and worth reading before touching a column. |
| `1. Order Details Apr'23 to June'23.xlsx`, `2. ... July'23 to Sept'23.xlsx` | ✅ exist (100 real rows each) | Real order-line data, actual `.xlsx` exports. |
| `3.`–`7.`, `10.`–`14. Order Details ...xlsx` | ⏳ placeholders (header row only, 0 rows) | Same real filenames as the user's actual data folder, numbering and periods verified against a screenshot of it. Auto-parse correctly via `db.discover_order_files()`. |
| `8. ...Jan'25 to Mar'25.xlsb`, `9. ...Apr'25 to June'25.xlsb`, `15. ...June'26 - July'26.xlsb` | ⏳ placeholders (header row only, 0 rows) | Same story as the `.xlsx` placeholders, but `.xlsb`. `pandas`/`pyxlsb` can only *read* `.xlsb`, not write one, so these were generated via Excel COM automation (`win32com`, requires Excel installed locally) instead of `pandas.to_excel`. Confirmed readable by the real pipeline (`pyxlsb` opens them fine — see `scripts/verify_data.py`'s Part 0). |
| **Local coverage** | — | All 15 real filenames are now mirrored locally (12 `.xlsx` + 3 `.xlsb`), Apr 2023 – Jul 2026 with zero gaps and zero files skipped by the parser. Only files `1` and `2` carry real sample rows; every other file is header-only until the user's real machine has real data — that's expected, not a bug. |
| `file_periods.csv` | ✅ exists (empty) | Manual override/addition for any filename the auto-parser can't handle. Not needed so far — every real filename has parsed cleanly. |
| `item_brand_mapping.csv` | ✅ exists (226 rows) | **Repurposed**: this is the *new-products list*, not a general brand map — see "Three tabs" below. Powers Tab 3 (NP Discounts) only. |
| `brand_master.csv` | ⏳ placeholder (header row only, `SKU_ID,BRAND_ID`) | The comprehensive old+new brand lookup for Tab 2 (Division Trend)'s Brand join. Every SKU shows as `Unmapped` until the real file lands — this is surfaced loudly in the dashboard's Data Notes banner, not silently wrong. |
| `New Medvol customers from 1st April 2025.csv` | ✅ exists (108 real rows) | Counter registration log — `Allocated_CounterCode` + `Request_CreatedDate` give each new counter's identity and birth date. Powers Tab 1's New/Old split (see `COUNTER_AGE_CUTOFF_DATE` below — currently unset, so every counter shows as "Old" regardless of this file's contents). |

### `scripts/` — code, no data lives here

| File | Status | What it does |
|------|--------|----------|
| `config.py` | ✅ exists | Every tunable value as a named constant: file paths, the 9 discount bucket edges/labels (`DISCOUNT_BUCKET_EDGES`/`LABELS` — the 65–80 gap is intentionally wider than the surrounding steps, confirmed by the user, not a typo), valid `Order_Status` values (`VALID_ORDER_STATUSES`) and excluded-status keywords, the canonical date column pair, join-key column names, `COUNTER_AGE_CUTOFF_DATE` (New/Old split — `None` = everyone "Old"; set a date string and rerun the build to activate), the Division Trend tab's intended window (`TREND_START_MONTH`/`TREND_END_MONTH`, currently Apr 2023 – Jul 2026), and the full 57-column `EXPECTED_COLUMNS` list used to detect schema drift. Change behavior by editing values here, not logic elsewhere. |
| `db.py` | ✅ exists | The only module that reads `data/` directly. Discovers order files by regex-parsing the real naming convention (`parse_period_from_filename`, with a `file_periods.csv` manual override), reads `.xlsx`/`.xlsb`/`.csv` (`_read_one_file`), strips stray whitespace from every string column (`strip_string_columns` — different export batches format cells differently), dedupes rows that appear identically across overlapping files while loudly flagging genuine value conflicts instead of guessing (`load_order_lines`), and loads the three reference files (new-products list, brand master, new-counters log) with the same "never silently drop data" discipline. Every file-read failure, schema mismatch, or data-quality issue anywhere in this module goes through `report_issue(severity, message)`, which both prints immediately and appends to the module-level `BUILD_ISSUES` list that ends up in the dashboard's red error banner. |
| `build_report_data.py` | ✅ exists | The one script you actually run (`python build_report_data.py`, no arguments). Calls `db.py` for data, classifies every row Valid/Excluded/Unclassified (whitespace/case-normalized status matching), computes derived columns (discount math, discount buckets, canonical transaction date, `Item_Description`/`Division_Name` canonicalization to fix casing-drift SKU-splitting), joins Brand and Counter_Age, aggregates into the three tabs' shapes, and writes `output/report_data.json` — then embeds that same JSON into `dashboard.html` so it also works opened directly via `file://`. Prints a timed progress line per pipeline stage (`stage()` context manager) so a long run never looks stuck. |
| `verify_data.py` | ✅ exists | Independent verification tool — deliberately does NOT import `build_report_data.py`'s classification/date/dedup logic; everything is reimplemented from scratch here so it's a real cross-check, not a tautology. Five checks, each wrapped so one crashing doesn't stop the others (`run_check`), with everything printed also saved to `output/verification_report.txt` (`log()`) so there's one pasteable file to hand back for diagnosis: **Part 0** confirms every single file discovered in `data/` — every order file (including 0-row placeholders) and all three reference files — actually opens, with row counts and column-mismatch flags (the only check guaranteed to mention every file by name; Parts A/B only see files that produced a sampled row). **Part D** checks period coverage purely from the discovered files' own declared filename periods (not the built report) — any month in the intended trend window with no file covering it, and any files with overlapping declared periods. **Part A** does row-level arithmetic sanity checks (`PTR × discounts × Qty ≈ Amount`, skipping `FixedPrice`-overridden rows) on random rows per file. **Part B** independently recomputes per-SKU-per-FY totals from raw data and reconciles them against `report_data.json`'s `division_tab`, catching bugs anywhere in the real pipeline (join, dedup, status filtering, aggregation). **Part C** pulls `report_data.json`'s own `meta.build_issues` into the same report. A final **SUMMARY** block tallies issue counts from every part into one scannable table (with a `<--` marker on any non-zero row) so "what's actually wrong with `data/` right now" has a one-glance answer instead of requiring a read-through of all five parts. Run with `python verify_data.py [--sample-size 100] [--seed 42]`; requires `report_data.json` to already exist. |

### `output/` — generated, safe to delete

| File | Status | What it is |
|------|--------|------------|
| `report_data.json` | ✅ generated by `build_report_data.py` | Small aggregated file with everything `dashboard.html` needs — pre-computed so the dashboard never has to touch the raw data files directly. Compact JSON (no pretty-printing) to keep file size sane at real scale. |
| `verification_report.txt` | ✅ generated by `verify_data.py` | Full text output of the last verification run — paste this back for diagnosis instead of terminal scrollback, which may be partial/stale. |

### Project root

| File | Status | What it is |
|------|--------|------------|
| `ARCHITECTURE.md` | ✅ exists | This file. |
| `dashboard.html` | ✅ exists | The static three-tab report (Discount Dispersion, Division Trend, NP Discounts). Reads embedded JSON from a `<script type="application/json" id="report-data">` tag when present (works via `file://`, no server needed); falls back to `fetch('./output/report_data.json')` for dev-server use. Self-contained single file — no external dependencies, hand-rolled SVG bar/line charts. |
| `requirements.txt` | ✅ exists | Pinned exact versions (`pandas==2.3.0`, `openpyxl==3.1.5`, `pyxlsb==1.0.10`) — added after a cross-machine run hung/behaved differently due to an unpinned environment missing `pyxlsb`. |
| `.gitignore` | ✅ exists | Excludes raw order files (`data/1*.xlsx`–`9*.xlsx`), `.xlsb` files, the new-counters CSV (contains real PII — phone/GST/drug-license numbers), `output/report_data.json`, and Python cache. `dashboard.html`'s embedded data is cleared via script before every push and restored locally right after — see the git workflow note below. |

## The three tabs this all serves

1. **Discount Dispersion tab** (`panel-counter` in the HTML, tab id `counter` for historical
   reasons) — sales value bucketed into 9 discount ranges (`config.DISCOUNT_BUCKET_LABELS`).
   Filters: **Counter Age** (Old/New checkboxes), **Discount Filter** (DiscountOnPTR-only vs.
   compounded Total Discount — two independent bucket sets, never combined), **Group table by**
   (SKU or Division — no Brand option here, see below), and a **Division** checklist with
   Select all/Deselect all. Table has its own text filter and shows top 50 by total sales.

   **New/Old resolved at build time, not live in the browser.** A live in-browser cutoff-date
   picker was tried first and abandoned: at real data scale (1.4M+ rows), shipping one JSON
   record per counter (`Doctor_Code`) for live classification produced 300k+ Counter tab
   records and made `dashboard.html` unopenable. Now `config.COUNTER_AGE_CUTOFF_DATE` is set
   once, `db.join_counter_age()` computes a single `Counter_Age` ("Old"/"New") column at build
   time, and only that 2-value column ships in the JSON. To change the cutoff: edit the config
   value and rerun `build_report_data.py` (a rebuild takes seconds, so this isn't a meaningful
   loss of flexibility). Currently `None` — every counter shows as "Old" — with an explicit note
   about that in the dashboard's Data Notes banner rather than it being silently misleading.

2. **Division Trend tab** — Medvol % (`sum(Amount)/sum(PTR×Quantity)`) and Net Sales %
   (`sum(InvoiceAmount)/sum(PTR×Quantity)`) over time, both shown alongside their underlying
   actual values (Amount, Invoice Amount, Gross Sales), not just the percentage. Drillable at
   **Division / Brand / SKU** level — this is the only tab with a Brand option, since
   `brand_master.csv` is only relevant here. Defaults to **Financial Year** view (Apr–Mar, FY
   label = the year it ends in) with a toggle to raw Month view; entity list defaults to top 5
   by sales with Select all/Deselect all. Two tables: totals across the shown period, and a
   per-period breakdown (both actual values + %), the latter with its own text filter.

3. **NP Discounts tab** — same 9 discount buckets as Tab 1, filtered to SKUs on the new-products
   list (`item_brand_mapping.csv` — repurposed as a new-products list, not a general catalog).
   **SKU level only** — no Division/Brand grouping option, per explicit instruction. Same
   DiscountOnPTR-only vs. Total Discount toggle as Tab 1.

**Brand is deliberately absent from Tabs 1 and 3** — `brand_master.csv` only feeds the Division
Trend tab's Brand drill-down; the other two tabs' JSON payloads (`counter_tab`,
`np_discounts_tab`) never include a brand field at all, per explicit instruction ("we only need
the brand view for the division trend tab, we don't need it for any other tabs").

## Known risks / edge cases — current status

These were identified early as ways the pipeline could silently produce wrong numbers. Status
of each, as actually implemented:

- **Reference-file joins silently dropping/inflating data — handled.** Unmatched `Item_Code`s
  in the Brand join get an explicit `"Unmapped"` label (`config.UNMAPPED_BRAND_LABEL`), never
  silently dropped, and the count is surfaced in the Data Notes banner. Duplicate keys in any
  reference file (new-products, brand master, new-counters) are detected and reported via
  `report_issue`, keeping the first occurrence rather than silently fanning out the join.

- **Division-by-zero / bad inputs in the discount math — handled.** `PTR`/`DiscountOnPTR`/
  `Cash_Discount` blanks are treated as 0 (`to_numeric(..., fill_value=0.0)`), explicit and
  documented, not left as NaN to silently propagate. Rows with a discount outside the valid
  0–100% range (negative, or a data-entry error above 100%) are excluded from the two
  discount-bucketed tabs — this is now reported via `report_issue` when it happens (fixed
  2026-08-25; previously this exclusion was silent).

- **Overlapping files with duplicate rows — handled.** `db.load_order_lines()` finds repeated
  `(Item_Code, Order_Number)` pairs across *different* source files; identical values silently
  keep one copy, but a genuine value conflict is flagged loudly via `report_issue`, listing
  sample keys, rather than picking either row.

- **Reruns must fully replace, not merge — handled.** `build_report_data.py` always reads all
  of `data/` fresh and overwrites `output/report_data.json` wholesale; nothing is patched
  incrementally.

- **A file with no manifest/parseable-filename entry — handled.** `db.discover_order_files()`
  logs and returns any such filenames (`skipped`), and `load_order_lines()` reports them as an
  `error`-severity build issue (data genuinely missing from the report), shown in the dashboard's
  red error banner, not just the terminal.

- **Gaps in coverage — handled, and currently empty.** `compute_missing_month_ranges()` checks
  every month in `config.TREND_START_MONTH`..`TREND_END_MONTH` against what's actually in
  `division_tab` and surfaces any gap explicitly in the Data Notes banner. On the user's real
  file layout (Apr 2023 – Jul 2026 across 15 files, verified against a screenshot), there are no
  gaps left once real data lands in all of them.

- **Source files are `.xlsx`/`.xlsb`, not `.csv` — handled.** `db._read_one_file()` branches by
  extension (`openpyxl` for `.xlsx`, `pyxlsb` for `.xlsb`, encoding-fallback CSV reader for
  `.csv`), all with `dtype=str` to avoid silent type coercion (leading zeros, date reformatting).

- **Date parsing ambiguity — handled.** `pd.to_datetime(..., errors="coerce")` on the canonical
  date column pair; unparseable dates become `NaT` and are explicitly excluded from the Division
  Trend tab (with a reported count), rather than corrupting month grouping with a broken `"NaT"`
  string (this was a real bug, found and fixed via `verify_data.py`).

- **Encoding artifacts — handled.** `.csv` reads try `config.CSV_ENCODING_FALLBACKS` in order
  (`utf-8`, `cp1252`, `latin-1`) instead of crashing or silently mangling text.

- **Status strings not seen in the small sample — handled.** `classify_status()` is
  substring/keyword-based for exclusions (`reject`, `cancel`) and normalizes whitespace/casing
  before matching, rather than requiring an exact match against every status string ever seen.
  Anything matching neither valid nor excluded patterns becomes `"Unclassified"` — visible in the
  Data Notes banner with a `"(blank)"` label for genuinely empty status values, never silently
  included or dropped.

## Git workflow

Push to `https://github.com/sussiboisource/Medvol_101.git` — but never push raw order files,
`.xlsb` files, `output/report_data.json`, or the new-counters CSV (real PII: phone/GST/drug
license numbers) — all excluded via `.gitignore`. Before every push, `dashboard.html`'s embedded
`<script id="report-data">` content is cleared (regex `re.subn` with a lambda replacement, not a
raw string, to avoid backslash-escape misinterpretation of `\u`-containing JSON), committed and
pushed, then `build_report_data.py` is rerun immediately locally to restore the real embedded
data. **Standing rule: never push without an explicit "push"/"push it" instruction in that turn**
— this repo's changes are not pushed proactively.
