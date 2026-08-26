"""Every tunable value for the Medvol dashboard pipeline. Edit values here, not the logic
elsewhere in db.py / build_report_data.py."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
REPORT_JSON_PATH = OUTPUT_DIR / "report_data.json"

FILE_PERIODS_MANIFEST = DATA_DIR / "file_periods.csv"

# Counter registration/creation-date log. Presence of a Doctor_Code here = "New" counter (as of
# its Request_CreatedDate); absence = "Old" by default. A future comprehensive counter master
# (old+new) may extend this later.
NEW_COUNTERS_FILE = DATA_DIR / "New Medvol customers from 1st April 2025.xlsx"
NEW_COUNTERS_ID_COLUMN = "Allocated_CounterCode"
NEW_COUNTERS_DATE_COLUMN = "Request_CreatedDate"

# Comprehensive old+new SKU -> Brand lookup, used for Tab 2 (Division Trend)'s Brand join.
BRAND_MASTER_FILE = DATA_DIR / "brand_master.csv"
BRAND_MASTER_SKU_COLUMN = "SKU_ID"
BRAND_MASTER_BRAND_COLUMN = "BRAND_ID"

# item_brand_mapping.csv is NOT a general brand map -- it's the new-products list that powers
# Tab 3 (NP Discounts). Presence of an Item_Code here = "this SKU is a new product."
NEW_PRODUCTS_FILE_PREFIX = "item_brand_mapping"

# Reference/lookup files are matched by prefix; anything else .csv/.xlsx in DATA_DIR is
# treated as raw order-line data.
NON_ORDER_FILE_PREFIXES = (
    "item_brand_mapping",
    "brand_master",
    "New Medvol customers",
    "file_periods",
)

# Editor lock/temp files that live alongside the real data but contain no data at all.
# Excel writes "~$<name>.xlsx" (a ~165-byte owner file) whenever a workbook is open; LibreOffice
# writes ".~lock.<name>#". These are NOT missing data -- they're an artifact of having the real
# file open. Treating them as order files produced two alarming-but-bogus errors on every build
# ("MISSED ENTIRE FILE ... PermissionError" and "MISSED 1 FILE(S) ... never even opened") while
# the real file right next to them loaded perfectly.
IGNORED_FILENAME_PREFIXES = ("~$", ".~lock.")

# Discount bucket edges, shared by both discount-range filters (DiscountOnPTR-only, and the
# compounded Total_Discount_Pct). Edges are in percent. The 65-80 gap is intentionally wider
# than the surrounding 10-point steps -- confirmed twice by the user, not a typo.
DISCOUNT_BUCKET_EDGES = [0, 5, 15, 25, 35, 45, 55, 65, 80, 100.0001]
DISCOUNT_BUCKET_LABELS = [
    "<5%", "5-15%", "15-25%", "25-35%", "35-45%",
    "45-55%", "55-65%", "65-80%", ">80%",
]

# Row classification: Order_Status values that count as a real, fulfilled sale.
VALID_ORDER_STATUSES = {"Fully Invoiced", "Partially Invoiced"}
# Status *substrings* (case-insensitive) that mean the order was cancelled/rejected and should
# be dropped entirely. Anything matching neither this nor VALID_ORDER_STATUSES is "Unclassified".
EXCLUDED_STATUS_KEYWORDS = ("reject", "cancel")

# Canonical transaction date: OrdPlaced_Date, falling back to Order_InitiatedDate when blank.
PRIMARY_DATE_COLUMN = "OrdPlaced_Date"
FALLBACK_DATE_COLUMN = "Order_InitiatedDate"

# Join keys.
COUNTER_ID_COLUMN = "Doctor_Code"
SKU_ID_COLUMN = "Item_Code"

# item_brand_mapping.csv's own columns (the new-products list, powers Tab 3).
NEW_PRODUCTS_SKU_COLUMN = "Code"
NEW_PRODUCTS_BRAND_COLUMN = "Brand"
NEW_PRODUCTS_DIVISION_COLUMN = "Division"
NEW_PRODUCTS_VERTICAL_COLUMN = "Vertical"

UNMAPPED_BRAND_LABEL = "Unmapped"

# New/Old counter age cutoff, computed ONCE at build time. Reverted from a live in-browser
# picker: at real data scale, shipping one JSON record per counter for live classification
# produced a 300k+ record Counter tab that made the dashboard unusable (see ARCHITECTURE.md).
# Three modes:
#   "auto"        -- use the EARLIEST parseable Request_CreatedDate found in the new-counters
#                    file itself as the cutoff. Since no row can be earlier than the earliest
#                    one, this makes every counter in that file "New" -- matches the file's own
#                    name ("New Medvol customers from 1st April 2025") without hand-picking a
#                    date. This is the default.
#   "YYYY-MM-DD"  -- a fixed cutoff date string, if you want something other than the file's
#                    own earliest date.
#   None          -- disable the split entirely; every counter shows as "Old".
# To change the mode, edit this and rerun the build script -- a rebuild takes seconds, so this
# is not a meaningful loss of flexibility in practice.
COUNTER_AGE_CUTOFF_DATE = "auto"

# Division Trend tab's intended time window. Informational only -- data outside this range
# is not clipped, just what the tab is meant to cover. FY27 is being treated as running through
# July 2026 for now (per user, matches the latest real file's coverage), not the full Mar 2027.
TREND_START_MONTH = "2023-04"
TREND_END_MONTH = "2026-07"

# Encodings to try in order when reading a raw CSV (xlsx doesn't need this).
CSV_ENCODING_FALLBACKS = ("utf-8", "cp1252", "latin-1")

# What to do when the same (Item_Code, Order_Number) appears in MORE THAN ONE file with
# DIFFERENT Amount/InvoiceAmount/Quantity/DiscountOnPTR values. This is common when a later
# export restates earlier orders (e.g. an order placed in Nov shipping in Jan gets its final
# invoice amount in the January file).
#   "keep_narrowest" -- (default) keep the copy from the file that covers the FEWEST months, on
#                  the reasoning that an export dedicated to one month is a better source for
#                  that month than a bulk export that merely happens to include it. Ties break
#                  toward the later period. This resolves the real dataset's problem without
#                  anyone having to re-export or rename anything.
#   "keep_latest" -- keep the copy from the file covering the LATEST months, on the reasoning
#                  that the newest export is the most restated/final.
#   "keep_all"  -- keep every copy. Nothing is guessed, but the line IS counted more than once,
#                  so every total is inflated by the duplicates. The historical behaviour;
#                  useful if you want to reconcile by hand rather than let a rule decide.
# Periods here are DERIVED FROM THE DATA (db.actual_period_from_data), not from the filename,
# so a mislabelled file cannot skew the choice. The build reports how many rows and how many
# rupees each decision moved.
DUPLICATE_CONFLICT_POLICY = "keep_narrowest"

# A file whose share of excluded (cancelled/rejected) rows differs wildly from the corpus norm
# is usually a differently-prepared export -- e.g. already pre-filtered to invoiced orders only.
# Not automatically wrong, but worth surfacing so it's a known fact rather than a surprise.
EXCLUSION_RATE_ANOMALY_TOLERANCE = 0.5  # flag files below 50% or above 200% of the overall rate

# The columns the pipeline's logic ACTUALLY reads. A file missing one of these is a real
# problem: the affected metric silently goes blank/NaN for that file's rows. Everything else in
# EXPECTED_COLUMNS below is just "what a typical export happens to contain" -- missing one of
# those changes no number anywhere.
#
# This distinction exists because the build used to warn about every column in EXPECTED_COLUMNS.
# Four real files legitimately lack "InvoiceAmount1" -- a column NOTHING in this codebase reads
# (the invoice math uses "InvoiceAmount", no suffix) -- so every build printed four scary
# warnings about data that was, in fact, completely fine. Real breakage was hidden in that noise.
CRITICAL_COLUMNS = [
    "Order_Number", "OrdPlaced_Date", "Order_InitiatedDate", "Doctor_Code", "Item_Code",
    "Item_Description", "Division_Name", "Quantity", "Amount", "InvoiceAmount", "Order_Status",
    "PTR", "DiscountOnPTR", "Cash_Discount",
]

# Columns that appear in some exports and not others, where the difference is known-harmless.
# Listed explicitly so a genuinely NEW unexpected absence still gets reported rather than being
# swept under the same rug.
KNOWN_OPTIONAL_COLUMNS = {
    "InvoiceAmount1",  # never read by any logic; "InvoiceAmount" is the one that matters
}

EXPECTED_COLUMNS = [
    "Order_Number", "Order_InitiatedDate", "OrdPlaced_Date", "Doctor_Code", "Doctor_Name",
    "CounterType", "City", "State", "LinkType", "PharmacyName", "Item_Code", "Stockist_Code",
    "Stockist_Name", "Item_Description", "Cluster_Name", "Division_Name", "Counter_Tag",
    "UIN_Code", "Alternate_UIN", "Quantity", "Free_Quantity", "Price", "Amount",
    "InvoiceAmount", "Order_Status", "RemarksName", "RemarksDesc", "InvoicedQty",
    "invoiced_Free_Quantity", "InvoiceAmount1", "Ordered_By", "DiscountOnPTR", "FixedPrice",
    "PTR", "Cash_Discount", "Source", "order_mode", "OrderReplenishement_Type", "Patient_Name",
    "GST_Customer", "Einv_Stockist", "sao_exempt", "einv_exempt", "eway_exempt",
    "L0_ShortCode", "L0_Name", "L1_ShortCode", "L1_Name", "L2_ShortCode", "L2_Name",
    "L3_ShortCode", "L3_Name", "HQ", "HeadQuarterName", "Location_Code", "Location_Name",
    "Position_Code",
]
