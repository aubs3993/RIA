"""Stage 2: parse the Form ADV xlsx into a clean dataframe.

Column naming in the SEC bulk file shifts over time (case, punctuation, suffixes
like '5F(2)(c)' vs '5F.(2)(c)'). We match by normalized regex prefix to stay
tolerant. Anything that fails to match is logged but does not crash the run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import config
from src.utils import get_logger, normalize_url

log = get_logger("parse_adv", config.LOG_DIR / "pipeline.log")


# (clean_name, list-of-regex-patterns matched against normalized column header)
# Patterns are tried in order; first column that matches wins.
COLUMN_RULES: list[tuple[str, list[str]]] = [
    # firm_legal_name MUST come before firm_dba so 'Legal Name' wins over
    # 'Primary Business Name' when both are present.
    ("firm_legal_name", [
        r"^1a(\b|[^0-9])", r"^1a$",
        r"^legal name$",
        r"legal.*name.*firm",
    ]),
    ("firm_dba", [
        r"^1b1\b", r"^1b\W*1\b",
        r"^primary business name$",
        r"primary.*business.*name",
    ]),
    ("sec_number", [
        r"^1d\b",
        r"^sec\s*#?$",
        r"^sec file",
        r"sec.*file.*number",
    ]),
    ("crd_number", [
        r"^1e1\b", r"^1e\W*1\b",
        r"\bcrd\b", r"organization.*crd",
    ]),
    # Office address block — Form ADV 1F1 OR new English headers ("Main Office ...")
    ("office_street", [
        r"^1f1.*street\W*1\b", r"^1f1.*street\b(?!.*2)",
        r"^main office street address 1$",
    ]),
    ("office_street2", [
        r"^1f1.*street\W*2\b",
        r"^main office street address 2$",
    ]),
    ("office_city", [
        r"^1f1.*city\b",
        r"^main office city$",
    ]),
    ("office_state", [
        r"^1f1.*state\b",
        r"^main office state$",
    ]),
    ("office_zip", [
        r"^1f1.*postal", r"^1f1.*zip",
        r"^main office postal",
    ]),
    ("office_country", [
        r"^1f1.*country\b",
        r"^main office country$",
    ]),
    ("office_phone", [
        r"^1f1.*tel", r"^1f1.*phone",
        r"^1g1?\b.*phone", r"^1g1?\b.*tel", r"^1g1?$",
        r"^main office telephone",
    ]),
    ("website", [
        r"^1i1\b", r"^1i\W*1\b", r"^1i\b.*website", r"website.*1i",
        r"^website address$",
    ]),
    # Item 5 — employees & client mix
    ("employee_count", [r"^5a\b"]),
    ("investment_advisory_employees", [r"^5b1\b", r"^5b\W*1\b"]),
    # Item 5D client mix: (1) = client count, (3) = AUM dollars.
    # (a) = individuals (non-HNW), (b) = HNW.
    ("individual_clients",     [r"^5d.*a.*1\b", r"^5da1\b"]),
    ("individual_aum_dollars", [r"^5d.*a.*3\b", r"^5da3\b"]),
    ("hnw_clients",            [r"^5d.*b.*1\b", r"^5db1\b"]),
    ("hnw_aum_dollars",        [r"^5d.*b.*3\b", r"^5db3\b"]),
    # AUM — 5F(2)(c) total regulatory AUM in dollars
    ("aum_total", [
        r"^5f.*2.*c\b", r"^5f2c\b", r"^5f\W*2\W*c\b",
        r"regulatory.*assets.*under.*mgmt", r"total.*regulatory.*aum",
    ]),
    ("total_accounts", [r"^5f.*2.*f\b", r"^5f2f\b", r"^5f\W*2\W*f\b"]),
    # Custody — old XLSX had a 9A yes/no flag; new CSV has 'Total Custody Amount' (numeric).
    # We accept either and convert to a boolean downstream.
    ("has_custody", [
        r"^9a\b", r"^9a1\b",
        r"^total custody amount$",
    ]),
]


@dataclass
class ColumnMapResult:
    mapping: dict[str, str]  # clean_name -> source_header
    unmatched_clean_names: list[str]
    sample_unmatched_source_headers: list[str]


def _normalize_header(h: object) -> str:
    """Lowercase, strip non-alphanumerics so '5F(2)(c)' and '5F.(2)(c)' compare equal."""
    if h is None:
        return ""
    s = str(h).strip().lower()
    # Keep word characters plus a few separators; drop punctuation that varies between exports.
    s = re.sub(r"[\s\-–—]+", " ", s)
    s = re.sub(r"[\.\(\)\[\],/]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_column_map(headers: list[str]) -> ColumnMapResult:
    norm_to_orig = {_normalize_header(h): h for h in headers}
    mapping: dict[str, str] = {}
    used_sources: set[str] = set()
    unmatched: list[str] = []

    for clean_name, patterns in COLUMN_RULES:
        match: str | None = None
        for pat in patterns:
            rx = re.compile(pat)
            for norm, orig in norm_to_orig.items():
                if orig in used_sources:
                    continue
                if rx.search(norm):
                    match = orig
                    break
            if match:
                break
        if match:
            mapping[clean_name] = match
            used_sources.add(match)
        else:
            unmatched.append(clean_name)

    sample_unmatched = [h for h in headers if h not in used_sources][:25]
    return ColumnMapResult(mapping=mapping, unmatched_clean_names=unmatched, sample_unmatched_source_headers=sample_unmatched)


def _coerce_number(s: pd.Series) -> pd.Series:
    """Parse currency/percentage-ish strings into floats. Bad values -> NaN."""
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce")
    cleaned = (
        s.astype("string")
        .str.replace(r"[\$,]", "", regex=True)
        .str.replace(r"\s+", "", regex=True)
        .str.replace("%", "", regex=False)
        .replace({"": None, "-": None, "N/A": None, "n/a": None})
    )
    return pd.to_numeric(cleaned, errors="coerce")


def _yes_no_to_bool(s: pd.Series) -> pd.Series:
    truthy = {"y", "yes", "true", "t", "1"}
    falsy = {"n", "no", "false", "f", "0"}

    def _conv(v: object) -> bool | None:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        t = str(v).strip().lower()
        if t in truthy:
            return True
        if t in falsy:
            return False
        return None

    return s.map(_conv).astype("boolean")


def _write_column_map_log(result: ColumnMapResult, source_path: Path) -> None:
    config.COLUMN_MAP_LOG.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Column mapping for {source_path.name}",
        f"# {len(result.mapping)} matched, {len(result.unmatched_clean_names)} unmatched clean names",
        "",
        "## Matched (clean_name <- source_header)",
    ]
    for clean, src in sorted(result.mapping.items()):
        lines.append(f"  {clean:<35} <- {src}")
    if result.unmatched_clean_names:
        lines += ["", "## UNMATCHED clean names (no source column found)"]
        lines += [f"  - {n}" for n in result.unmatched_clean_names]
    if result.sample_unmatched_source_headers:
        lines += ["", "## Sample unused source headers (first 25)"]
        lines += [f"  - {h}" for h in result.sample_unmatched_source_headers]
    config.COLUMN_MAP_LOG.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote column-mapping log → %s", config.COLUMN_MAP_LOG)


def _read_source(path: Path) -> pd.DataFrame:
    """Read either an xlsx or a csv from the SEC bulk file."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return pd.read_excel(path, dtype=object, engine="openpyxl")
    if suffix == ".csv":
        # The SEC's CSV is latin-1 encoded; utf-8 trips on a few firm names.
        return pd.read_csv(path, dtype=object, encoding="latin-1", low_memory=False)
    raise ValueError(f"Unsupported source file type: {path.suffix} ({path.name})")


def parse_adv(source_path: Path) -> pd.DataFrame:
    """Load xlsx/csv, map columns, normalize types, write parquet, return df."""
    config.ensure_dirs()
    log.info("Reading %s ...", source_path)
    df_raw = _read_source(source_path)
    log.info("Loaded %d rows × %d columns", len(df_raw), df_raw.shape[1])

    cmap = _build_column_map(list(df_raw.columns))
    _write_column_map_log(cmap, source_path)

    for clean in cmap.unmatched_clean_names:
        log.warning("No source column matched for '%s' — downstream will see NaN", clean)

    out = pd.DataFrame(index=df_raw.index)
    for clean, src in cmap.mapping.items():
        out[clean] = df_raw[src]

    # Fill any unmatched clean columns with NaN so downstream code can rely on shape.
    for clean in cmap.unmatched_clean_names:
        out[clean] = pd.NA

    # String columns
    for col in [
        "firm_legal_name", "firm_dba", "sec_number", "crd_number",
        "office_street", "office_street2", "office_city", "office_state",
        "office_zip", "office_country", "office_phone",
    ]:
        if col in out.columns:
            out[col] = out[col].astype("string").str.strip()

    # Upper-case state codes for clean joins/filters.
    if "office_state" in out.columns:
        out["office_state"] = out["office_state"].str.upper()

    # Normalize websites
    if "website" in out.columns:
        out["website"] = out["website"].astype("string").map(normalize_url, na_action="ignore").astype("string")

    # Numeric columns (dollars / counts / employees)
    for col in [
        "employee_count", "investment_advisory_employees",
        "individual_aum_dollars", "hnw_aum_dollars",
        "aum_total", "total_accounts",
    ]:
        if col in out.columns:
            out[col] = _coerce_number(out[col])

    # Client counts → nullable Int64 (so missing stays NA, not silently 0)
    for col in ["individual_clients", "hnw_clients"]:
        if col in out.columns:
            out[col] = _coerce_number(out[col]).astype("Int64")

    # Custody flag — new CSV ships a numeric 'Total Custody Amount';
    # old XLSX shipped a Yes/No 9A column. Detect which we have.
    if "has_custody" in out.columns:
        s = out["has_custody"]
        if s.dtype == object or pd.api.types.is_string_dtype(s):
            # Try numeric first (handles "$1,200,000" strings); fall back to yes/no.
            numeric = _coerce_number(s)
            if numeric.notna().any():
                out["has_custody"] = numeric.fillna(0).gt(0).astype("boolean")
            else:
                out["has_custody"] = _yes_no_to_bool(s)
        elif pd.api.types.is_numeric_dtype(s):
            out["has_custody"] = s.fillna(0).gt(0).astype("boolean")
        else:
            out["has_custody"] = _yes_no_to_bool(s)

    # Reorder for readability
    preferred = [
        "firm_legal_name", "firm_dba", "sec_number", "crd_number",
        "office_street", "office_street2", "office_city", "office_state", "office_zip", "office_country",
        "office_phone", "website",
        "employee_count", "investment_advisory_employees",
        "individual_clients", "individual_aum_dollars",
        "hnw_clients", "hnw_aum_dollars",
        "aum_total", "total_accounts", "has_custody",
    ]
    cols = [c for c in preferred if c in out.columns] + [c for c in out.columns if c not in preferred]
    out = out[cols]

    out_path = config.CLEAN_PARQUET
    out.to_parquet(out_path, index=False)
    log.info("Wrote clean dataframe → %s (%d rows)", out_path, len(out))
    print(f"[parse_adv] rows={len(out)} cols={out.shape[1]} → {out_path}")
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        candidates = [p for p in config.RAW_DIR.iterdir() if p.suffix.lower() in (".xlsx", ".csv")]
        if not candidates:
            raise SystemExit(f"No xlsx/csv in {config.RAW_DIR}")
        path = max(candidates, key=lambda p: p.stat().st_mtime)
    parse_adv(path)
