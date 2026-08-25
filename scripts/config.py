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
NEW_COUNTERS_FILE = DATA_DIR / "New Medvol customers from 1st April 2025.csv"
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

# New/Old counter age is decided live in the dashboard (viewer picks a cutoff date), not baked
# in at build time -- see ARCHITECTURE.md. build_report_data.py ships each counter's raw
# creation date; the browser does the New/Old split.

# Division Trend tab's intended time window. Informational only -- data outside this range
# is not clipped, just what the tab is meant to cover.
TREND_START_MONTH = "2023-04"
TREND_END_MONTH = "2026-05"

# Encodings to try in order when reading a raw CSV (xlsx doesn't need this).
CSV_ENCODING_FALLBACKS = ("utf-8", "cp1252", "latin-1")

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
