"""Every tunable value for the Medvol dashboard pipeline. Edit values here, not the logic
elsewhere in db.py / build_report_data.py."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
REPORT_JSON_PATH = OUTPUT_DIR / "report_data.json"

FILE_PERIODS_MANIFEST = DATA_DIR / "file_periods.csv"
COUNTER_CREATION_DATES_FILE = DATA_DIR / "counter_creation_dates.csv"

# Reference/lookup files are matched by prefix; anything else .csv/.xlsx in DATA_DIR is
# treated as raw order-line data.
BRAND_MAPPING_FILE_PREFIXES = ("item_brand_mapping",)
NON_ORDER_FILE_PREFIXES = (
    "item_brand_mapping",
    "counter_creation_dates",
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
BRAND_MAPPING_SKU_COLUMN = "Code"
BRAND_MAPPING_BRAND_COLUMN = "Brand"
BRAND_MAPPING_DIVISION_COLUMN = "Division"
BRAND_MAPPING_VERTICAL_COLUMN = "Vertical"

UNMAPPED_BRAND_LABEL = "Unmapped"
UNKNOWN_COUNTER_AGE_LABEL = "Unknown"

# New/Old counter split. No creation-date file yet, so every counter is "Old" and this cutoff
# is unused. Once the file exists, set this (or pass a cutoff into build_report_data.py) and
# counters missing from the creation-date file get UNKNOWN_COUNTER_AGE_LABEL, never a silent
# default.
COUNTER_AGE_CUTOFF_DATE = None

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
