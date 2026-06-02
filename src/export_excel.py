"""Export the enriched master CSV to a clean .xlsx that Excel won't mangle.

Opening the CSV directly in Excel strips the leading zero from zip codes and
turns long IDs / phone numbers into scientific notation. This writes an xlsx
where:
  - ID/zip/phone columns are real TEXT cells (leading zeros kept, no sci-notation)
  - dollars stay numeric with thousands separators; counts/flags stay integers
  - match_score shows 3 decimals
  - the header row is bold + frozen, the firm-name column is frozen, and an
    autofilter is applied so the sheet is sortable/filterable on open.

Usage:  python -m src.export_excel
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

import config

# Must stay TEXT so Excel doesn't strip leading zeros / use scientific notation.
TEXT_COLS = ["crd_number", "sec_number", "office_zip", "office_phone"]
# Integer columns that can contain blanks -> nullable Int (no trailing ".0").
INT_COLS = [
    "employee_count", "investment_advisory_employees", "individual_clients",
    "hnw_clients", "total_accounts", "Zomma Priority", "Zomma Fit",
]


def export(csv_path: Path | None = None, xlsx_path: Path | None = None) -> Path:
    csv_path = Path(csv_path) if csv_path else (config.ENRICHED_DIR / "ria_master_20260504.csv")
    xlsx_path = Path(xlsx_path) if xlsx_path else csv_path.with_suffix(".xlsx")

    # Read the ID/zip/phone columns as strings up front so leading zeros survive.
    df = pd.read_csv(csv_path, dtype={c: str for c in TEXT_COLS}, low_memory=False)

    # Clean the text columns: blank out missing, strip a stray trailing ".0".
    for c in TEXT_COLS:
        if c in df.columns:
            s = df[c].where(df[c].notna(), "").astype(str).str.strip()
            df[c] = s.str.replace(r"\.0$", "", regex=True).replace({"nan": ""})

    # Counts / flags / scores -> nullable Int64 so blanks stay blank, not "1.0".
    int_cols = INT_COLS + [c for c in df.columns if c.startswith("svc_")]
    for c in int_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")

    dollar_cols = [c for c in df.columns if "aum" in c.lower() or "dollars" in c.lower()]

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as xl:
        df.to_excel(xl, index=False, sheet_name="RIA Master")
        ws = xl.sheets["RIA Master"]
        col_of = {cell.value: cell.column for cell in ws[1]}

        # Header: bold + frozen (row 1 and the firm-name column A); autofilter.
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(vertical="center")
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions

        def set_format(colname: str, number_format: str) -> None:
            if colname not in col_of:
                return
            letter = get_column_letter(col_of[colname])
            for cell in ws[letter][1:]:  # skip header
                cell.number_format = number_format

        for c in TEXT_COLS:
            set_format(c, "@")               # explicit text format
        for c in dollar_cols:
            set_format(c, "#,##0")           # 1,117,783,829
        set_format("match_score", "0.000")

        # Reasonable, capped auto-width per column.
        for col_name, col_idx in col_of.items():
            letter = get_column_letter(col_idx)
            sample = df[col_name].astype(str).head(400)
            width = max(len(str(col_name)), int(sample.str.len().max() or 0)) + 2
            ws.column_dimensions[letter].width = min(max(width, 10), 45)

    print(f"Wrote {xlsx_path}  ({len(df):,} rows x {df.shape[1]} cols)")
    return xlsx_path


if __name__ == "__main__":
    export()
