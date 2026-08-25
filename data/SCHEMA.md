# Data Schema — Medvol Order-Line Data

This documents every column in the raw order-line CSVs (`orders_*.csv` / `order_lines_sample.csv`),
based on the 98-row sample. Raw files are never edited — this doc is the map of what's inside them.

**Row grain**: one row = one item within one order. `Order_Number` repeats across every item
in the same order (e.g. `ORD3112230454` appears 5 times, once per item). The real row key is
`Order_Number + Item_Code`, not `Order_Number` alone.

## Column-by-column

| # | Column | Role | Meaning |
|---|--------|------|---------|
| 1 | `Order_Number` | ID | Order-level identifier (repeats per line item) |
| 2 | `Order_InitiatedDate` | Date | Timestamp the order was started |
| 3 | `OrdPlaced_Date` | Date | Timestamp the order was confirmed/placed; **blank when the order was rejected/never placed** |
| 4 | `Doctor_Code` | ID | The counter/customer identifier (e.g. `DR14360`) — **candidate join key for the future counter-creation-date file** |
| 5 | `Doctor_Name` | Dimension | Counter's display name |
| 6 | `CounterType` | Dimension | `DR` / `PH` / `HS` (doctor / pharmacy / hospital) |
| 7 | `City` | Dimension | |
| 8 | `State` | Dimension | |
| 9 | `LinkType` | Dimension | Long-form counter type: Doctor attached Pharmacy, Standalone Pharmacy, Hospital Pharmacy, Dispensing counter |
| 10 | `PharmacyName` | Dimension | Attached pharmacy name; blank for dispensing-counter type |
| 11 | `Item_Code` | ID | SKU identifier |
| 12 | `Stockist_Code` | ID | Distributor identifier |
| 13 | `Stockist_Name` | Dimension | |
| 14 | `Item_Description` | Dimension | Product name — SKU-level grain |
| 15 | `Cluster_Name` | Dimension | **= `Vertical`** (confirmed by user) — the top level of the product hierarchy. In the 98-row sample it happened to equal `Division_Name` every time; that looks like sample coincidence rather than the real rule, since the brand-mapping file's own `Vertical` values (`Acute_1`, `Chronic`) look nothing like `Division_Name` values (`Maximus`, `Grandera`). Treat as unverified until seen on fuller data. |
| 16 | `Division_Name` | Dimension | The division/brand grouping (e.g. Maximus, Zenura, Optimus) — **this drives the Division Trend tab** |
| 17 | `Counter_Tag` | Dimension | Blank in sample |
| 18 | `UIN_Code` | ID | Numeric code, likely a session/cart transaction ID — not used by either tab |
| 19 | `Alternate_UIN` | — | Blank in sample |
| 20 | `Quantity` | Measure | Units ordered |
| 21 | `Free_Quantity` | Measure | Free units (0 throughout sample) |
| 22 | `Price` | Measure | Per-unit price after `DiscountOnPTR` = `PTR × (1 − DiscountOnPTR/100)` |
| 23 | `Amount` | Measure | Final line value = price after `Cash_Discount` × `Quantity`. **This is "sales" for the Counter tab.** |
| 24 | `InvoiceAmount` | Measure | Actually billed value; blank when nothing was invoiced |
| 25 | `Order_Status` | Status | Sample shows `Fully Invoiced`, `Partially Invoiced`, **and `Order Rejected by ASM`** ⚠️ (see flags below) |
| 26 | `RemarksName` | — | Blank in sample |
| 27 | `RemarksDesc` | — | Blank in sample |
| 28 | `InvoicedQty` | Measure | Quantity actually invoiced (≤ `Quantity` for partials) |
| 29 | `invoiced_Free_Quantity` | Measure | Blank/0 in sample |
| 30 | `InvoiceAmount1` | Measure | Unit price × quantity, before `Cash_Discount` |
| 31 | `Ordered_By` | Dimension | Who placed the order: `PH` / `DR` / `DL` |
| 32 | `DiscountOnPTR` | Measure | % discount off `PTR` — **drives Filter A** |
| 33 | `FixedPrice` | Measure | 0 throughout sample |
| 34 | `PTR` | Measure | Base price to retailer, per unit |
| 35 | `Cash_Discount` | Measure | Second discount %, applied after `DiscountOnPTR` — feeds **Filter B** (Total Discount) |
| 36 | `Source` | Metadata | App used to place the order |
| 37 | `order_mode` | — | Blank in sample |
| 38 | `OrderReplenishement_Type` | Dimension | `Regular Order` throughout sample |
| 39 | `Patient_Name` | — | Blank in sample |
| 40 | `GST_Customer` | Dimension | GST vs Non-GST counter |
| 41 | `Einv_Stockist` | Metadata | |
| 42 | `sao_exempt` | Flag | `N` throughout sample |
| 43 | `einv_exempt` | Flag | `N` throughout sample |
| 44 | `eway_exempt` | Flag | `N` throughout sample |
| 45–52 | `L0_ShortCode`/`L0_Name` … `L3_ShortCode`/`L3_Name` | Dimension | ⚠️ **Not a brand hierarchy** — sample values are person names (e.g. "Sandesh Arvind Tise"). Looks like a field-staff/sales-org reporting chain, not product levels. See flags below. |
| 53 | `HQ` | ID | Headquarters code |
| 54 | `HeadQuarterName` | Dimension | |
| 55 | `Location_Code` | ID | |
| 56 | `Location_Name` | Dimension | Often matches `PharmacyName`, sometimes differs |
| 57 | `Position_Code` | ID | Looks like a per-line internal reference code |

## ⚠️ Two things worth flagging before we build further

1. **`Order_Status` has a third value in the sample: `Order Rejected by ASM`.** You said earlier
   only "Partially Invoiced" or "Fully Invoiced" would appear — the sample has rejected rows too
   (no `InvoiceAmount`, no `OrdPlaced_Date`). These represent orders that never actually sold, so
   the pipeline's filter to `{Fully Invoiced, Partially Invoiced}` will correctly drop them — just
   flagging that the assumption doesn't hold in this sample, in case that changes what you want
   counted anywhere else.

2. **`L0`–`L3` are not the brand/SKU hierarchy** — they're a chain of person names, most likely the
   sales reporting structure (rep → area manager → regional manager → zonal, or similar) attached
   to whoever's territory the order falls under. The brand/SKU level for the Division Trend tab
   should come from `Division_Name` (brand) and `Item_Description` (SKU) instead — not `L0`–`L3`.

## Reference file: Item → Brand mapping

The main order-line export has **no usable Brand column** — `Division_Name`/`Cluster_Name` exist,
but real brand-level granularity is missing, and `L0`–`L3` are not brand levels (see flag above).
The user has a separate mapping file, sample saved at `data/item_brand_mapping_sample.csv`:

| Column | Meaning |
|--------|---------|
| `Code` | Item/SKU code — joins to the order data's `Item_Code` |
| `Product Description` | Product name |
| `Vertical` | Top-level grouping (e.g. `Acute_1`, `Chronic`) |
| `Division` | Same concept as order data's `Division_Name`, but not always an identical string (e.g. `Derma_B` here vs `Derma B` in order data) |
| `Franchise` | Sub-brand grouping |
| `Brand` | The actual brand name — this is what's missing from the main export |

**Only 8 sample rows exist so far** — the full item→brand list is still pending from the user, so
any brand-level testing right now is necessarily partial. Join key: `Item_Code` (orders) =
`Code` (this file).

## Product hierarchy (confirmed by user)

```
Vertical (= Cluster_Name)
  └── Division (Division_Name / Division in brand file)
        └── Brand (Brand, from brand-mapping file only)
```

Strict 1:N:N nesting — one Vertical has many Divisions, one Division has many Brands. `Item_Code`
(SKU) is a **unique, stable identifier per product** — one code always means one specific product,
safe to use as the join key with no ambiguity.

`Brand` and `HQ`/`HeadQuarterName` are **not** part of this hierarchy — their relationship is
many-to-many (a brand sells through many HQs, an HQ sells many brands). It only *looks* like
one-to-many because there are far more HQs than brands numerically — don't build any logic that
assumes a brand belongs to a single HQ.

## Division Trend tab metrics

Two percentage metrics, both aggregated at Division/Brand/SKU × month grain as
`sum(numerator) / sum(denominator)` (not a row-by-row average — weighted by value, so a few huge
orders don't get diluted by many tiny ones):

- **Medvol %** = `sum(Amount) / sum(PTR × Quantity)`. `Amount` is sales value after *both*
  `DiscountOnPTR` and `Cash_Discount`; `PTR × Quantity` is the undiscounted gross value. Per user:
  "discounted sales / gross sales."
- **Net Sales %** = `sum(InvoiceAmount) / sum(PTR × Quantity)` — **confirmed**. Nets out both
  discounts and the shortfall from partially-invoiced orders.

Both are shown as toggleable options on the same trend chart, not combined into one number.

## Row classification (3-way, confirmed)

Every row gets classified into exactly one bucket:

1. **Valid** — `Order_Status` is `Fully Invoiced` or `Partially Invoiced`. Counted normally in
   both tabs. Verified against the 98-row sample: `InvoiceAmount` presence agrees with this
   classification on all 98 rows (69 Fully + 5 Partially Invoiced, all with real `InvoiceAmount`;
   24 Order Rejected by ASM, all blank) — so checking `InvoiceAmount` non-zero is kept as a silent
   backstop in case the full data has an `Order_Status` string never seen in the sample.
2. **Excluded** — `Order_Status` matches a cancelled/rejected pattern (`Order Rejected by ASM` is
   the only one seen so far; the full data may also have `Cancelled`-type statuses). Dropped
   entirely, everywhere — never counted.
3. **Unclassified** — anything that's neither of the above (a status string not yet seen, meaning
   neither "clearly valid" nor "clearly cancelled/rejected"). **Not silently included, not
   silently dropped** — kept as its own visible category so an unexpected status in the full data
   shows up as something to investigate, rather than quietly skewing totals either direction.

## Canonical transaction date

For any date-based logic (monthly trend bucketing, resolving which of two duplicate rows to keep
across overlapping files): use `OrdPlaced_Date`; if that's blank, fall back to
`Order_InitiatedDate` (which is populated on every sample row, so this fallback always resolves).

## Multi-file "database" behaviour

Once monthly/quarterly files land (`orders_2023-12.csv`, `orders_2024-Q1.csv`, etc.), they all
share this exact schema. They stay as separate files on disk — a data-access layer (planned as
`scripts/db.py`, not yet written) will be the only code that reads `data/`, and will:
- discover every `orders_*.csv` by filename pattern and load them into one in-memory table
- load `counter_creation_dates.csv` when present and expose a join on `Doctor_Code`
- hand that combined table to `build_report_data.py` — so raw files never merge on disk, only in memory, and nothing downstream needs to know how many files or which period they came from
