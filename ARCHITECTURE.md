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
| `SCHEMA.md` | ✅ exists | Data dictionary: every column across all raw files, what it means, and the confirmed product hierarchy (Vertical → Division → Brand). Read this before writing any logic that touches a column. |
| `order_lines_sample.csv` | ✅ exists | 98-row sample of order-line data, for building/testing against before the full 600MB export arrives. Never edited after creation. |
| `Order Details <Mon>'<YY> to <Mon>'<YY>.xlsx` (real filenames, kept as-is) | ⏳ planned | Real order-line data, **as actual .xlsx exports** from the source system (not clean CSVs — same schema as the sample once read in). Periods are irregular: mostly quarterly, but some single months, and at least one span covering 10 months. See "File-period manifest" below for how these get tracked. |
| `file_periods.csv` | ⏳ planned | The manifest: `filename → period_start, period_end`. Since the real filenames aren't consistently parseable (irregular spans, human-written), each new file's date range is confirmed by the user in conversation once, then recorded here — the pipeline reads this manifest instead of trying to regex-parse filenames. |
| `item_brand_mapping_sample.csv` | ✅ exists | 8-row sample of the Item_Code → Brand/Franchise/Vertical/Division mapping. The main order export has no Brand column, so this is required for brand-level reporting. |
| `item_brand_mapping.csv` | ⏳ planned | Full item→brand list, replacing the sample once the user provides it. |
| `counter_creation_dates.csv` | ⏳ planned | Doctor_Code → creation date. Drives the New/Old counter split on the Counter tab. Until this exists, every counter is treated as "Old". |

### `scripts/` — code, no data lives here

| File | Status | What it does |
|------|--------|----------|
| `config.py` | ⏳ planned | Every tunable value as a named constant: the 9 discount bucket edges, the New/Old cutoff date, file-naming patterns, valid `Order_Status` values to keep. Change behavior by editing values here, not logic elsewhere. |
| `db.py` | ⏳ planned | Data-access layer. Reads `file_periods.csv` to know which raw `.xlsx`/`.csv` file covers which date range, loads and stacks them into one in-memory table, loads `counter_creation_dates.csv` and `item_brand_mapping*.csv` and knows how to join them in (on `Doctor_Code` and `Item_Code` respectively). Nothing else in the project reads `data/` directly — everything asks this module instead. |
| `build_report_data.py` | ⏳ planned | Calls `db.py` for data, computes derived columns (`Total_Discount_Pct`, discount buckets, Medvol %, Net Sales %), filters to invoiced order statuses, aggregates into the two tabs' shapes, and writes `output/report_data.json`. This is the only script you actually run. |

### `output/` — generated, safe to delete

| File | Status | What it is |
|------|--------|----------|
| `report_data.json` | ⏳ planned | Small aggregated file produced by `build_report_data.py`. Contains everything `dashboard.html` needs — pre-computed so the dashboard never has to touch a 600MB CSV directly. |

### Project root

| File | Status | What it is |
|------|--------|----------|
| `ARCHITECTURE.md` | ✅ exists | This file. |
| `dashboard.html` | ⏳ planned | The static two-tab report (Counter tab, Division Trend tab). Reads only `output/report_data.json`. Publishable as a link, viewable without running any Python. |

## The two tabs this all serves

1. **Counter tab** — sales value bucketed into discount ranges. Selection: **New / Old counter
   checkboxes** (multi-selectable — check one or both; a 3rd selection is planned for later but
   not yet defined). Results then grouped by **SKU or Brand** (user's choice). Two independent
   discount-bucket filters (DiscountOnPTR-only vs. combined Total Discount) as before.

   **Resolved**: the dataset is static for now — no live upload pipeline, no in-browser prompt.
   Whenever a new file (data or cutoff-date-relevant) is added, the user tells the assistant
   directly in conversation what it means, and the manifest/config gets updated by hand at that
   point. Every counter is "Old" for now regardless, since there's no creation-date file yet.
2. **Division Trend tab** — Medvol % and Net Sales % (see `data/SCHEMA.md` for the formulas) over
   time (Apr 2023–May 2026), drillable at Division / Brand / SKU level.

## Known risks / edge cases to design around

These aren't hypothetical — they're specific ways the pipeline could silently produce wrong
numbers instead of erroring loudly. `build_report_data.py` and `db.py` should defend against all
of them:

- **Reference-file joins can silently drop or inflate data.**
  - Item→Brand mapping only has 8 rows today. Any `Item_Code` not found in it must land in an
    explicit **"Unmapped"** bucket at Brand level — never silently vanish from totals. SKU-level
    and Division-level views are unaffected either way since they don't depend on this join.
  - Same for counter-creation-dates once it exists: a `Doctor_Code` missing from that file should
    show as an explicit **"Unknown"** age, not silently default to "Old" — silent defaulting would
    hide genuinely-new counters that just haven't hit the reference file yet.
  - Before joining, assert each reference file's key (`Item_Code`/`Doctor_Code`) is unique —
    duplicate keys fan out the join and quietly inflate sales totals.

- **Division-by-zero / bad inputs in the discount math.** `PTR`, `DiscountOnPTR`, `Cash_Discount`
  can be blank, zero, or (in bad rows) negative/>100. Because Medvol %/Net Sales % are computed as
  `sum(numerator)/sum(denominator)` rather than row-by-row division, a handful of bad rows won't
  crash the whole aggregate — but blank values need an explicit "treat as 0" rule so they don't
  propagate as NaN and zero out an entire month/division's sum.

- **Overlapping files contain duplicate rows — dedupe them, don't just warn.** The user's real
  file list has `Order Details Aug'25 to May'26.xlsx` alongside separate `Oct'25`/`Nov'25`/`Dec'25`
  files that genuinely overlap, and confirmed the overlapping months' data is the same in both
  places. So after stacking files, `db.py` should find repeated `(Order_Number, Item_Code)` pairs
  across *different* files and **keep only one copy**. Defensive check: if two "duplicate" rows
  for the same key actually have different values (e.g. a different `Amount`), that contradicts
  the "same data" assumption — flag that specific case loudly rather than silently picking either
  one, since it would mean the files disagree, not just repeat.

- **"Updating data" means reruns must fully replace, not merge.** Since a period file might get
  replaced with corrected numbers later, `output/report_data.json` must be fully regenerated and
  overwritten on every run — never patched incrementally — so corrected data can't end up
  blended with stale numbers.

- **A file with no manifest entry.** Since periods come from `file_periods.csv`, not filename
  parsing, a file dropped into `data/` without a corresponding manifest row would otherwise go
  unread. `db.py` should log every file it finds in `data/` and flag any without a manifest entry,
  so a missed file is loud, not silent.

- **Gaps in coverage are expected, not bugs.** The real file list has a real gap — nothing covers
  Oct'24 through Jul'25. The Division Trend tab should show a genuine gap in the timeline for
  those months rather than interpolating or erroring. When a file eventually fills a gap, it
  should slot in normally on the next rebuild — no special-case code needed for "the first time a
  gap gets filled."

- **Source files are `.xlsx`, not `.csv`.** Reading them needs `pandas.read_excel` (openpyxl)
  rather than the CSV reader — worth checking column types survive the read the same way (Excel
  can silently coerce things like leading-zero codes or dates differently than a CSV would).

- **Date parsing ambiguity.** Raw dates look like `12/31/2023 22:46` (US month/day/year). This
  needs an explicit date format on parse — letting pandas guess risks silently swapping day/month
  for some rows.

- **Encoding artifacts in the source data.** At least one sample product name shows a mangled
  character (`Xyzal Nasal Spray 27.5?Cg`), suggesting the source export isn't cleanly UTF-8.
  Reading raw files needs a fallback encoding strategy rather than crashing or silently mangling
  more text.

- **The sample is 98 rows; the full data may have statuses/edge cases we haven't seen.** The
  row-validity rule (keep only rows with non-zero `InvoiceAmount`) is deliberately status-agnostic
  for this reason — it doesn't rely on having seen every possible `Order_Status` string.
