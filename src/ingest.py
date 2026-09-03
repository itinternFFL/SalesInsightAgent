"""Load and clean the monthly sales-report Excel files in data/ into one tidy DataFrame.

Each file (Sale_Report_FMO-<Mon>-<Year>.xlsx) has two sheets, "Cereals" and
"Dairy", with slightly different column layouts. This module normalizes both
into a single schema and concatenates everything into one master table.

Also provides parse_upload() - the entry point for validating and cleaning a
file uploaded mid-conversation before it's accepted into data/.
"""

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
MASTER_PARQUET = CACHE_DIR / "master_sales.parquet"

MAX_HEADER_SCAN_ROWS = 10  # how far down to search for the real header row
HEADER_ANCHORS = {"Month", "Invoice No"}  # first-cell values that mark a header row

MONTH_ABBR_TO_NUM = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
NUM_TO_MONTH_ABBR = {v: k for k, v in MONTH_ABBR_TO_NUM.items()}

# Common output schema every sheet gets mapped onto.
COMMON_COLUMNS = [
    "month", "date", "category", "invoice_no", "customer_name", "channel",
    "sale_type", "material_code", "mat_name", "brand", "gross_sales",
    "invoice_discounts", "quantity", "kgs_litres", "net_sale", "source_file",
]

# Expected raw columns per sheet (after header-row stripping), used to
# validate new uploads before they're trusted enough to merge.
EXPECTED_COLUMNS = {
    "Cereals": [
        "Month", "Invoice No", "Sales Order No.", "DC No", "PO No", "Date",
        "customer code", "customer name", "Channel", "Material", "mat name",
        "Brand", "Gross Sales Local", "Invoice Discounts",
        "Mega Distributor Margin", "Sales Tax",
        "Advance Income Tax u/s 236 (g)/(h)", "Quantity", "C.F",
        "kgs/litres", "Net Sale",
    ],
    "Dairy": [
        "Invoice No", "Date", "Month", "customer code", "customer name",
        "Channel", "Sale Type", "Material", "mat name", "Brand",
        "Gross Sales Local", "Invoice Discounts", "Mega Distributor Margin",
        "Sales Tax", "Advance Income Tax u/s 236 (g)/(h)", "Quantity", "C.F",
        "kgs/litres", "Amount in Local Currency",
    ],
}


def _month_from_filename(path: Path) -> str | None:
    """Sale_Report_FMO-Apr-2026.xlsx -> '2026-04' (used instead of the
    inconsistent in-sheet Month columns, which are a string in Cereals and a
    datetime in Dairy). Returns None if the filename doesn't match."""
    match = re.search(r"-([A-Za-z]{3})-(\d{4})", path.stem)
    if not match:
        return None
    abbr, year = match.group(1).title(), match.group(2)
    if abbr not in MONTH_ABBR_TO_NUM:
        return None
    return f"{year}-{MONTH_ABBR_TO_NUM[abbr]:02d}"


def canonical_filename(month: str) -> str:
    """'2026-04' -> 'Sale_Report_FMO-Apr-2026.xlsx' - the inverse of
    _month_from_filename(), used to name an upload that doesn't already
    follow the convention."""
    year, num = month.split("-")
    return f"Sale_Report_FMO-{NUM_TO_MONTH_ABBR[int(num)]}-{year}.xlsx"


def _month_from_sheet_content(raw_no_header: pd.DataFrame) -> str | None:
    """Fallback for when the filename doesn't follow the naming convention:
    every sheet's title rows contain a cell like 'FMO-Apr-2026' (row 1,
    column 0, confirmed in every existing file) - search the first few rows
    for that pattern."""
    for i in range(min(MAX_HEADER_SCAN_ROWS, len(raw_no_header))):
        cell = raw_no_header.iat[i, 0]
        if not isinstance(cell, str):
            continue
        match = re.search(r"([A-Za-z]{3})-(\d{4})", cell)
        if match:
            abbr, year = match.group(1).title(), match.group(2)
            if abbr in MONTH_ABBR_TO_NUM:
                return f"{year}-{MONTH_ABBR_TO_NUM[abbr]:02d}"
    return None


def _find_header_row(raw_no_header: pd.DataFrame) -> int | None:
    """Locate the real header row by scanning the first few rows for one
    whose first cell is a recognizable field name, rather than assuming a
    fixed row number - so a file with an extra title row, or one fewer,
    still parses correctly."""
    for i in range(min(MAX_HEADER_SCAN_ROWS, len(raw_no_header))):
        cell = raw_no_header.iat[i, 0]
        if isinstance(cell, str) and cell.strip() in HEADER_ANCHORS:
            return i
    return None


def validate_columns(actual: list[str], expected: list[str]) -> list[str]:
    """Compare actual vs. expected columns for a sheet, returning
    human-readable issue descriptions (empty list if they match)."""
    actual_set, expected_set = set(actual), set(expected)
    issues = []
    missing = expected_set - actual_set
    extra = actual_set - expected_set
    if missing:
        issues.append(f"missing expected columns: {', '.join(sorted(missing))}")
    if extra:
        issues.append(f"unexpected new columns: {', '.join(sorted(extra))}")
    return issues


def _safe_col(df: pd.DataFrame, name: str) -> pd.Series:
    """Column access that degrades to all-NaN instead of raising, so an
    upload with a missing/renamed column (flagged separately by
    validate_columns) can still be cleaned - needed for the "merge anyway"
    path, where the user has already accepted the schema warning."""
    if name in df.columns:
        return df[name]
    return pd.Series([None] * len(df), index=df.index)


def _clean_cereals(df: pd.DataFrame, month: str, source_file: str) -> pd.DataFrame:
    out = pd.DataFrame({
        "month": month,
        "date": pd.to_datetime(_safe_col(df, "Date"), errors="coerce"),
        "category": "Cereals",
        "invoice_no": _safe_col(df, "Invoice No").astype(str),
        "customer_name": _safe_col(df, "customer name"),
        "channel": _safe_col(df, "Channel"),
        "sale_type": None,  # not present in the Cereals sheet
        "material_code": _safe_col(df, "Material"),
        "mat_name": _safe_col(df, "mat name"),
        "brand": _safe_col(df, "Brand"),
        "gross_sales": pd.to_numeric(_safe_col(df, "Gross Sales Local"), errors="coerce"),
        "invoice_discounts": pd.to_numeric(_safe_col(df, "Invoice Discounts"), errors="coerce"),
        "quantity": pd.to_numeric(_safe_col(df, "Quantity"), errors="coerce"),
        "kgs_litres": pd.to_numeric(_safe_col(df, "kgs/litres"), errors="coerce"),
        "net_sale": pd.to_numeric(_safe_col(df, "Net Sale"), errors="coerce"),
        "source_file": source_file,
    })
    return out


def _clean_dairy(df: pd.DataFrame, month: str, source_file: str) -> pd.DataFrame:
    out = pd.DataFrame({
        "month": month,
        "date": pd.to_datetime(_safe_col(df, "Date"), errors="coerce"),
        "category": "Dairy",
        "invoice_no": _safe_col(df, "Invoice No").astype(str),
        "customer_name": _safe_col(df, "customer name"),
        "channel": _safe_col(df, "Channel"),
        "sale_type": _safe_col(df, "Sale Type"),
        "material_code": _safe_col(df, "Material"),
        "mat_name": _safe_col(df, "mat name"),
        "brand": _safe_col(df, "Brand"),
        "gross_sales": pd.to_numeric(_safe_col(df, "Gross Sales Local"), errors="coerce"),
        "invoice_discounts": pd.to_numeric(_safe_col(df, "Invoice Discounts"), errors="coerce"),
        "quantity": pd.to_numeric(_safe_col(df, "Quantity"), errors="coerce"),
        "kgs_litres": pd.to_numeric(_safe_col(df, "kgs/litres"), errors="coerce"),
        "net_sale": pd.to_numeric(_safe_col(df, "Amount in Local Currency"), errors="coerce"),
        "source_file": source_file,
    })
    return out


CLEANERS = {"Cereals": _clean_cereals, "Dairy": _clean_dairy}


def _load_file(path: Path) -> pd.DataFrame:
    month = _month_from_filename(path)
    if month is None:
        raise ValueError(f"Could not parse month/year from filename: {path.name}")
    frames = []
    xl = pd.ExcelFile(path)
    for sheet_name in xl.sheet_names:
        if sheet_name not in CLEANERS:
            continue
        raw_no_header = xl.parse(sheet_name, header=None, nrows=MAX_HEADER_SCAN_ROWS)
        header_row = _find_header_row(raw_no_header)
        if header_row is None:
            raise ValueError(f"Could not find header row in {path.name} / {sheet_name}")
        raw = xl.parse(sheet_name, header=header_row)
        raw.columns = raw.columns.str.strip()
        frames.append(CLEANERS[sheet_name](raw, month, path.name))
    return pd.concat(frames, ignore_index=True)


def _cache_is_fresh(xlsx_files: list[Path]) -> bool:
    if not MASTER_PARQUET.exists():
        return False
    cache_mtime = MASTER_PARQUET.stat().st_mtime
    return all(f.stat().st_mtime <= cache_mtime for f in xlsx_files)


def load_all(data_dir: Path = DATA_DIR, use_cache: bool = True) -> pd.DataFrame:
    """Load every monthly sales report into one tidy DataFrame, caching the
    result to cache/master_sales.parquet so repeat runs skip re-parsing xlsx."""
    xlsx_files = sorted(data_dir.glob("*.xlsx"))
    if not xlsx_files:
        raise FileNotFoundError(f"No .xlsx files found in {data_dir}")

    if use_cache and _cache_is_fresh(xlsx_files):
        return pd.read_parquet(MASTER_PARQUET)

    frames = [_load_file(path) for path in xlsx_files]
    df = pd.concat(frames, ignore_index=True)

    # Drop rows that are entirely blank (spreadsheet padding) and rows with
    # no customer name (not a real transaction line).
    df = df.dropna(how="all", subset=["customer_name", "brand", "net_sale"])
    df = df.reset_index(drop=True)

    CACHE_DIR.mkdir(exist_ok=True)
    df.to_parquet(MASTER_PARQUET, index=False)
    return df


@dataclass
class SheetParseResult:
    sheet_name: str
    category: str
    df: pd.DataFrame
    row_count: int  # after dropping blank rows
    dropped_blank_rows: int
    duplicate_rows: int
    positive_sign_rows: int
    schema_issues: list[str] = field(default_factory=list)


@dataclass
class UploadParseResult:
    filename: str
    month: str | None  # None if undetectable from filename or content
    sheets: list[SheetParseResult] = field(default_factory=list)

    @property
    def has_schema_issues(self) -> bool:
        return any(s.schema_issues for s in self.sheets)

    @property
    def combined_df(self) -> pd.DataFrame:
        return pd.concat([s.df for s in self.sheets], ignore_index=True)


def parse_upload(file_bytes: bytes, filename: str) -> UploadParseResult:
    """Validate and clean an uploaded file the same way an existing data/
    file is loaded, without touching disk. Used by the upload endpoints to
    decide whether a file can merge straight in or needs the user's input
    first (naming conflict, schema mismatch, undetectable period)."""
    month = _month_from_filename(Path(filename))

    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    sheets = []

    for sheet_name in xl.sheet_names:
        if sheet_name not in CLEANERS:
            continue

        raw_no_header = xl.parse(sheet_name, header=None, nrows=MAX_HEADER_SCAN_ROWS)

        if month is None:
            month = _month_from_sheet_content(raw_no_header)

        header_row = _find_header_row(raw_no_header)
        if header_row is None:
            sheets.append(SheetParseResult(
                sheet_name=sheet_name, category=sheet_name, df=pd.DataFrame(),
                row_count=0, dropped_blank_rows=0, duplicate_rows=0,
                positive_sign_rows=0,
                schema_issues=["could not locate a header row in this sheet"],
            ))
            continue

        raw = xl.parse(sheet_name, header=header_row)
        raw.columns = raw.columns.str.strip()

        schema_issues = validate_columns(
            list(raw.columns), EXPECTED_COLUMNS.get(sheet_name, list(raw.columns))
        )

        cleaned = CLEANERS[sheet_name](raw, month or "unknown", filename)
        total_before = len(cleaned)
        blank_mask = cleaned[["customer_name", "brand", "net_sale"]].isna().all(axis=1)
        cleaned = cleaned[~blank_mask].reset_index(drop=True)

        sheets.append(SheetParseResult(
            sheet_name=sheet_name,
            category=sheet_name,
            df=cleaned,
            row_count=len(cleaned),
            dropped_blank_rows=int(blank_mask.sum()),
            duplicate_rows=int(cleaned.duplicated().sum()),
            positive_sign_rows=int((cleaned["net_sale"] > 0).sum()),
            schema_issues=schema_issues,
        ))

    return UploadParseResult(filename=filename, month=month, sheets=sheets)


if __name__ == "__main__":
    data = load_all(use_cache=False)
    print(f"Loaded {len(data):,} rows from {data['source_file'].nunique()} files")
    print(f"Months: {sorted(data['month'].unique())}")
    print(f"Categories: {sorted(data['category'].unique())}")
    print(data[COMMON_COLUMNS].dtypes)
    print(data[["net_sale", "quantity"]].describe())
