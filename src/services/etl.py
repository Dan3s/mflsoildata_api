"""
Shared ETL engine for the bulk upload flow (carga de datos).

The same parser/validator is used by both ``POST /upload/validate`` (dry-run) and
``POST /upload/import`` (commit) so that "what was validated" is byte-for-byte
"what gets imported", as required by the data dictionary
(``DATA_DICTIONARY_soil_platform_v6.md``).

Column id conventions used across the template, validation and import:

- ``var:<variable_id>``      -> a measurement over a catalog variable.
- ``attr:<ATTRIBUTE_CODE>``  -> an EAV attribute of the sample.
- flat code (``site_code``)  -> a structural field (project/site/event/sample).
"""

from __future__ import annotations

import io
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook, load_workbook
from sqlalchemy import func

from mflsoildata_orm.models import (
    Measurement,
    Project,
    Sample,
    SampleAttribute,
    SampleAttributeValue,
    SampleType,
    SamplingEvent,
    Site,
    Variable,
)

# ---------------------------------------------------------------------------
# Constants & catalog of structural columns
# ---------------------------------------------------------------------------

TEMPLATE_SHEET = "Plantilla"
META_SHEET = "_meta"

# Tokens that mean "no value".
MISSING_TOKENS = {"", "NA", "N/A", "-", "--", "NULL", "NONE", "."}

# Qualifiers recognised in measurement cells (data dictionary 2.3).
QUALIFIER_RE = re.compile(r"^\s*(<|>)\s*(.+)$")


@dataclass
class StructuralColumn:
    id: str
    label: str
    required: bool
    value_type: str  # numeric | text | date
    db_target: str
    example: Optional[str] = None
    unit: Optional[str] = None


# Structural block. The first 8 are the mandatory "Identificación" columns from
# the frontend docs; the rest are optional but commonly available.
STRUCTURAL_COLUMNS: List[StructuralColumn] = [
    StructuralColumn("project_code", "Código de Proyecto", True, "text", "projects.code", "CLOC-CO"),
    StructuralColumn("project_name", "Nombre del Proyecto", True, "text", "projects.name", "Clima-LoCa Colombia"),
    StructuralColumn("site_code", "Código del Sitio", True, "text", "sites.site_code", "S-001"),
    StructuralColumn("latitude", "Latitud del Sitio", True, "numeric", "sites.geom", "4.6097"),
    StructuralColumn("longitude", "Longitud del Sitio", True, "numeric", "sites.geom", "-74.0817"),
    StructuralColumn("event_date", "Fecha de Muestreo", True, "date", "sampling_events.event_date", "2024-04-15"),
    StructuralColumn("sample_code", "Código de Muestra", True, "text", "samples.sample_code", "M-001"),
    StructuralColumn("sample_type_code", "Tipo de Muestra", True, "text", "samples.sample_type_id", "SOIL"),
    # Optional structural columns
    StructuralColumn("project_country", "País del Proyecto", False, "text", "projects.country", "Colombia"),
    StructuralColumn("project_donor", "Donante", False, "text", "projects.donor"),
    StructuralColumn("site_country", "País del Sitio", False, "text", "sites.country", "Colombia"),
    StructuralColumn("admin_1", "Admin 1 (Departamento)", False, "text", "sites.admin_1"),
    StructuralColumn("admin_2", "Admin 2 (Municipio)", False, "text", "sites.admin_2"),
    StructuralColumn("admin_3", "Admin 3", False, "text", "sites.admin_3"),
    StructuralColumn("altitude_m", "Altitud (m)", False, "numeric", "sites.altitude_m", unit="m"),
    StructuralColumn("event_activity", "Actividad del Evento", False, "text", "sampling_events.activity"),
    StructuralColumn("event_type", "Tipo de Evento", False, "text", "sampling_events.event_type"),
]

STRUCTURAL_BY_ID: Dict[str, StructuralColumn] = {c.id: c for c in STRUCTURAL_COLUMNS}
REQUIRED_STRUCTURAL_IDS = [c.id for c in STRUCTURAL_COLUMNS if c.required]


def is_known_id(token: str) -> bool:
    """Does ``token`` look like a technical column id (used for header detection)?"""
    if not token:
        return False
    token = token.strip()
    return token in STRUCTURAL_BY_ID or token.startswith("var:") or token.startswith("attr:")


# ---------------------------------------------------------------------------
# Normalisation helpers (data dictionary 2.2)
# ---------------------------------------------------------------------------


def normalize_code(value: Any) -> Optional[str]:
    """Uppercase, strip accents, collapse symbols/spaces to ``_``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text)
    text = text.strip("_")
    return text or None


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
    if text == "" or text.upper() in MISSING_TOKENS:
        return None
    return text


def parse_numeric(raw: Any) -> Tuple[Optional[Decimal], Optional[str], bool]:
    """Return (value, qualifier, ok). ``ok`` False means the token was unparseable."""
    if raw is None:
        return None, None, True
    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw)), None, True
    text = str(raw).strip()
    if text == "" or text.upper() in MISSING_TOKENS:
        # "ND" is a non-detect qualifier rather than a plain missing value.
        if text.upper() == "ND":
            return None, "ND", True
        return None, None, True

    qualifier: Optional[str] = None
    match = QUALIFIER_RE.match(text)
    if match:
        qualifier = match.group(1)
        text = match.group(2).strip()

    # decimal comma -> dot (only when there is no dot already)
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text), qualifier, True
    except (InvalidOperation, ValueError):
        return None, qualifier, False


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%m/%d/%Y")


def parse_date(raw: Any) -> Tuple[Optional[date], bool]:
    if raw is None:
        return None, True
    if isinstance(raw, datetime):
        return raw.date(), True
    if isinstance(raw, date):
        return raw, True
    text = str(raw).strip()
    if text == "" or text.upper() in MISSING_TOKENS:
        return None, True
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date(), True
        except ValueError:
            continue
    return None, False


def parse_boolean(raw: Any) -> Tuple[Optional[bool], bool]:
    if raw is None:
        return None, True
    if isinstance(raw, bool):
        return raw, True
    text = str(raw).strip().lower()
    if text == "" or text.upper() in MISSING_TOKENS:
        return None, True
    if text in {"true", "1", "si", "sí", "yes", "y", "verdadero"}:
        return True, True
    if text in {"false", "0", "no", "n", "falso"}:
        return False, True
    return None, False


# ---------------------------------------------------------------------------
# Column specs (resolved against the DB catalogs)
# ---------------------------------------------------------------------------


@dataclass
class ColumnSpec:
    id: str
    kind: str  # 'structural' | 'attr' | 'var'
    label: str
    value_type: str
    required: bool = False
    code: Optional[str] = None       # structural key / attribute_code / variable_code
    ref_id: Optional[int] = None     # variable_id or sample_attribute_id
    unit: Optional[str] = None
    db_target: Optional[str] = None


def resolve_columns(db, ids: List[str]) -> Tuple[List[ColumnSpec], List[dict]]:
    """Resolve raw column ids to specs, reporting unknown variables/attributes."""
    issues: List[dict] = []
    specs: List[ColumnSpec] = []

    # Pre-load referenced catalogs in bulk.
    var_ids = []
    attr_codes = []
    for cid in ids:
        if cid.startswith("var:"):
            try:
                var_ids.append(int(cid.split(":", 1)[1]))
            except ValueError:
                pass
        elif cid.startswith("attr:"):
            attr_codes.append(cid.split(":", 1)[1])

    variables = {v.id: v for v in db.query(Variable).filter(Variable.id.in_(var_ids)).all()} if var_ids else {}
    attributes = (
        {a.sample_attribute_code: a for a in db.query(SampleAttribute).filter(SampleAttribute.sample_attribute_code.in_(attr_codes)).all()}
        if attr_codes
        else {}
    )

    for cid in ids:
        cid = cid.strip()
        if not cid:
            continue
        if cid in STRUCTURAL_BY_ID:
            sc = STRUCTURAL_BY_ID[cid]
            specs.append(
                ColumnSpec(
                    id=cid, kind="structural", label=sc.label, value_type=sc.value_type,
                    required=sc.required, code=cid, unit=sc.unit, db_target=sc.db_target,
                )
            )
        elif cid.startswith("var:"):
            raw = cid.split(":", 1)[1]
            try:
                vid = int(raw)
            except ValueError:
                issues.append({"row": 0, "column": cid, "value": cid, "message": "Identificador de variable inválido", "severity": "error", "code": "UNKNOWN_COLUMN"})
                continue
            variable = variables.get(vid)
            if not variable:
                issues.append({"row": 0, "column": cid, "value": cid, "message": f"Variable {vid} no existe en el catálogo", "severity": "error", "code": "UNKNOWN_VARIABLE"})
                continue
            specs.append(
                ColumnSpec(
                    id=cid, kind="var", label=variable.name, value_type=variable.value_type,
                    code=variable.variable_code, ref_id=variable.id, db_target="measurements",
                )
            )
        elif cid.startswith("attr:"):
            code = cid.split(":", 1)[1]
            attribute = attributes.get(code)
            if not attribute:
                issues.append({"row": 0, "column": cid, "value": cid, "message": f"Atributo {code} no existe en el catálogo", "severity": "error", "code": "UNKNOWN_ATTRIBUTE"})
                continue
            specs.append(
                ColumnSpec(
                    id=cid, kind="attr", label=attribute.name, value_type=attribute.value_type,
                    code=attribute.sample_attribute_code, ref_id=attribute.id, db_target="sample_attribute_values",
                )
            )
        else:
            issues.append({"row": 0, "column": cid, "value": cid, "message": "Columna no reconocida", "severity": "warning", "code": "UNKNOWN_COLUMN"})

    return specs, issues


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------


@dataclass
class ParsedFile:
    column_ids: List[str]
    rows: List[Dict[str, Any]]  # each row: {column_id: raw_value, "_row": int}


def _detect_id_row(rows: List[List[Any]]) -> int:
    """Return the index of the row that holds technical ids, or -1 if not found."""
    for idx, row in enumerate(rows[:2]):  # only the first two rows can be headers
        tokens = [str(c).strip() for c in row if c not in (None, "")]
        if not tokens:
            continue
        known = sum(1 for t in tokens if is_known_id(t))
        if known and known >= max(1, len(tokens) // 2):
            return idx
    return -1


def parse_file(content: bytes, filename: str, columns_hint: Optional[List[str]] = None) -> ParsedFile:
    """Parse an uploaded xlsx/csv file into ordered column ids + raw rows."""
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        raw_rows = _read_csv(content)
    else:
        raw_rows = _read_xlsx(content)

    raw_rows = [r for r in raw_rows if any(c not in (None, "") for c in r)]
    if not raw_rows:
        raise ValueError("El archivo no contiene filas.")

    id_row_idx = _detect_id_row(raw_rows)
    if id_row_idx >= 0:
        column_ids = [str(c).strip() if c is not None else "" for c in raw_rows[id_row_idx]]
        data_start = id_row_idx + 1
    elif columns_hint:
        column_ids = [c.strip() for c in columns_hint]
        data_start = 1  # assume a single human-readable header row
    else:
        raise ValueError(
            "No se pudieron identificar las columnas. Use la plantilla generada por /upload/template "
            "o envíe el parámetro 'columns'."
        )

    rows: List[Dict[str, Any]] = []
    for offset, raw in enumerate(raw_rows[data_start:], start=data_start):
        # Skip comment rows (e.g. the "# Label" documentation row in CSV templates).
        non_empty = [str(c).strip() for c in raw if c not in (None, "")]
        if non_empty and all(c.startswith("#") for c in non_empty):
            continue
        row: Dict[str, Any] = {"_row": offset + 1}  # 1-based spreadsheet line
        has_value = False
        for col_idx, cid in enumerate(column_ids):
            if not cid:
                continue
            value = raw[col_idx] if col_idx < len(raw) else None
            if isinstance(value, str):
                value = value.strip()
            if value not in (None, ""):
                has_value = True
            row[cid] = value
        if has_value:
            rows.append(row)

    return ParsedFile(column_ids=[c for c in column_ids if c], rows=rows)


def _read_csv(content: bytes) -> List[List[Any]]:
    import csv

    text = content.decode("utf-8-sig", errors="replace")
    # Sniff delimiter (comma vs semicolon).
    sample = text[:4096]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [list(r) for r in reader]


def _read_xlsx(content: bytes) -> List[List[Any]]:
    wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    sheet_names = [s for s in wb.sheetnames if s != META_SHEET]
    ws = wb[TEMPLATE_SHEET] if TEMPLATE_SHEET in wb.sheetnames else wb[sheet_names[0]]
    return [list(r) for r in ws.iter_rows(values_only=True)]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _value_issue(row_num: int, column: str, value: Any, message: str, severity: str, code: str) -> dict:
    return {"row": row_num, "column": column, "value": None if value is None else str(value), "message": message, "severity": severity, "code": code}


def validate(
    parsed: ParsedFile,
    specs: List[ColumnSpec],
    known_sample_type_codes: Optional[set] = None,
) -> Tuple[List[dict], dict, int]:
    """Validate parsed rows against column specs.

    ``known_sample_type_codes`` is the set of sample_type codes already in the
    catalog. Unknown codes are reported as a single warning per code (they are
    tolerated and created on import), not silently created.

    Returns (issues, summary, valid_row_count).
    """
    issues: List[dict] = []
    spec_by_id = {s.id: s for s in specs}

    present_ids = set(parsed.column_ids)
    missing_required = [cid for cid in REQUIRED_STRUCTURAL_IDS if cid not in present_ids]
    for cid in missing_required:
        issues.append(_value_issue(0, cid, None, f"Falta la columna obligatoria '{cid}'", "error", "MISSING_COLUMN"))

    projects: set = set()
    sites: set = set()
    events: set = set()
    sample_keys: set = set()
    measurement_count = 0
    rows_with_error: set = set()
    unknown_sample_types: dict = {}  # normalized code -> first raw value seen

    for row in parsed.rows:
        row_num = row["_row"]

        # --- structural fields ---
        project_code = normalize_code(row.get("project_code"))
        site_code = row.get("site_code")
        sample_code = row.get("sample_code")

        for cid in REQUIRED_STRUCTURAL_IDS:
            if cid in missing_required:
                continue
            if _clean_str(row.get(cid)) is None:
                issues.append(_value_issue(row_num, cid, row.get(cid), f"'{cid}' es obligatorio", "error", "REQUIRED"))
                rows_with_error.add(row_num)

        # lat / lon
        lat = lon = None
        if "latitude" in present_ids:
            lat, _, ok = parse_numeric(row.get("latitude"))
            if not ok:
                issues.append(_value_issue(row_num, "latitude", row.get("latitude"), "Valor no numérico", "error", "NOT_NUMERIC"))
                rows_with_error.add(row_num)
            elif lat is not None and not (Decimal("-90") <= lat <= Decimal("90")):
                issues.append(_value_issue(row_num, "latitude", row.get("latitude"), "Latitud fuera de rango (-90 a 90)", "error", "OUT_OF_RANGE"))
                rows_with_error.add(row_num)
        if "longitude" in present_ids:
            lon, _, ok = parse_numeric(row.get("longitude"))
            if not ok:
                issues.append(_value_issue(row_num, "longitude", row.get("longitude"), "Valor no numérico", "error", "NOT_NUMERIC"))
                rows_with_error.add(row_num)
            elif lon is not None and not (Decimal("-180") <= lon <= Decimal("180")):
                issues.append(_value_issue(row_num, "longitude", row.get("longitude"), "Longitud fuera de rango (-180 a 180)", "error", "OUT_OF_RANGE"))
                rows_with_error.add(row_num)
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 90 and abs(lat) > 50 and abs(lon) < 20:
            issues.append(_value_issue(row_num, "latitude", row.get("latitude"), "¿Latitud/longitud invertidas?", "warning", "POSSIBLE_SWAP"))

        # event_date
        event_date = None
        if "event_date" in present_ids:
            event_date, ok = parse_date(row.get("event_date"))
            if not ok:
                issues.append(_value_issue(row_num, "event_date", row.get("event_date"), "Fecha inválida", "error", "INVALID_DATE"))
                rows_with_error.add(row_num)

        # altitude
        if "altitude_m" in present_ids and _clean_str(row.get("altitude_m")) is not None:
            _, _, ok = parse_numeric(row.get("altitude_m"))
            if not ok:
                issues.append(_value_issue(row_num, "altitude_m", row.get("altitude_m"), "Valor no numérico", "error", "NOT_NUMERIC"))
                rows_with_error.add(row_num)

        # sample_type: warn (not silently create) when the code is unknown.
        if known_sample_type_codes is not None and "sample_type_code" in present_ids:
            stc = normalize_code(row.get("sample_type_code"))
            if stc and stc not in known_sample_type_codes and stc not in unknown_sample_types:
                unknown_sample_types[stc] = row.get("sample_type_code")

        # uniqueness tracking (within file)
        if project_code:
            projects.add(project_code)
            if site_code:
                sites.add((project_code, normalize_code(site_code)))
                events.add((project_code, normalize_code(site_code), event_date, normalize_code(row.get("event_activity"))))
                sk = (project_code, normalize_code(site_code), event_date, sample_code)
                if sk in sample_keys:
                    issues.append(_value_issue(row_num, "sample_code", sample_code, "Muestra duplicada en el archivo (event_id, sample_code)", "error", "DUPLICATE"))
                    rows_with_error.add(row_num)
                sample_keys.add(sk)

        # --- EAV attributes & variables ---
        for cid in parsed.column_ids:
            spec = spec_by_id.get(cid)
            if spec is None or spec.kind == "structural":
                continue
            raw_value = row.get(cid)
            if _clean_str(raw_value) is None:
                continue
            if spec.kind == "var":
                measurement_count += 1
            _validate_typed_cell(row_num, spec, raw_value, issues, rows_with_error)

    for stc, raw in unknown_sample_types.items():
        issues.append(_value_issue(0, "sample_type_code", raw, f"El tipo de muestra '{stc}' no existe en el catálogo; se creará al importar.", "warning", "NEW_SAMPLE_TYPE"))

    total_rows = len(parsed.rows)
    has_structure_errors = bool(missing_required)
    valid_rows = total_rows - len(rows_with_error)
    if has_structure_errors:
        valid_rows = 0

    summary = {
        "projects": len(projects),
        "sites": len(sites),
        "sampling_events": len(events),
        "samples": len(sample_keys),
        "measurements": measurement_count,
        "new_variables": 0,
    }
    return issues, summary, max(valid_rows, 0)


def _validate_typed_cell(row_num: int, spec: ColumnSpec, raw_value: Any, issues: List[dict], rows_with_error: set) -> None:
    if spec.value_type == "numeric":
        value, qualifier, ok = parse_numeric(raw_value)
        if not ok:
            issues.append(_value_issue(row_num, spec.id, raw_value, "Valor no numérico", "error", "NOT_NUMERIC"))
            rows_with_error.add(row_num)
            return
        # Light range check for pH-like variables.
        if value is not None and spec.code and spec.code.upper().startswith("PH"):
            if not (Decimal("0") <= value <= Decimal("14")):
                issues.append(_value_issue(row_num, spec.id, raw_value, "pH fuera de rango (0-14)", "warning", "OUT_OF_RANGE"))
    elif spec.value_type == "date":
        _, ok = parse_date(raw_value)
        if not ok:
            issues.append(_value_issue(row_num, spec.id, raw_value, "Fecha inválida", "error", "INVALID_DATE"))
            rows_with_error.add(row_num)
    elif spec.value_type == "boolean":
        _, ok = parse_boolean(raw_value)
        if not ok:
            issues.append(_value_issue(row_num, spec.id, raw_value, "Valor booleano inválido", "error", "NOT_BOOLEAN"))
            rows_with_error.add(row_num)
    # text: anything is acceptable


# ---------------------------------------------------------------------------
# Template generation
# ---------------------------------------------------------------------------


def build_template(specs: List[ColumnSpec], fmt: str, include_examples: bool) -> Tuple[bytes, str, str]:
    """Return (content, media_type, filename)."""
    if fmt == "csv":
        return _build_template_csv(specs, include_examples)
    return _build_template_xlsx(specs, include_examples)


def _example_for(spec: ColumnSpec) -> str:
    if spec.kind == "structural":
        sc = STRUCTURAL_BY_ID.get(spec.id)
        if sc and sc.example:
            return sc.example
        if spec.value_type == "numeric":
            return "0"
        if spec.value_type == "date":
            return "2024-04-15"
        return ""
    if spec.value_type == "numeric":
        return "0"
    if spec.value_type == "date":
        return "2024-04-15"
    if spec.value_type == "boolean":
        return "true"
    return ""


def _build_template_xlsx(specs: List[ColumnSpec], include_examples: bool) -> Tuple[bytes, str, str]:
    wb = Workbook()
    ws = wb.active
    ws.title = TEMPLATE_SHEET

    labels = [s.label for s in specs]
    ids = [s.id for s in specs]
    ws.append(labels)   # row 1: human labels
    ws.append(ids)      # row 2: technical ids (hidden, machine-readable)
    ws.row_dimensions[2].hidden = True
    if include_examples:
        ws.append([_example_for(s) for s in specs])
    ws.freeze_panes = "A3"

    # _meta sheet for documentation / robust re-mapping.
    meta = wb.create_sheet(META_SHEET)
    meta.append(["id", "label", "required", "value_type", "unit", "db_target"])
    for s in specs:
        meta.append([s.id, s.label, "yes" if s.required else "no", s.value_type, s.unit or "", s.db_target or ""])

    buffer = io.BytesIO()
    wb.save(buffer)
    return (
        buffer.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "plantilla_carga.xlsx",
    )


def _build_template_csv(specs: List[ColumnSpec], include_examples: bool) -> Tuple[bytes, str, str]:
    import csv

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    # CSV cannot hide rows, so the header row IS the technical id (self-describing
    # for re-upload). A second commented line documents the human label.
    writer.writerow([s.id for s in specs])
    writer.writerow([f"# {s.label}" for s in specs])
    if include_examples:
        writer.writerow([_example_for(s) for s in specs])
    return buffer.getvalue().encode("utf-8-sig"), "text/csv", "plantilla_carga.csv"


# ---------------------------------------------------------------------------
# In-memory upload store (TTL) used to bridge validate -> import
# ---------------------------------------------------------------------------


@dataclass
class StoredUpload:
    upload_id: str
    filename: str
    column_ids: List[str]
    rows: List[Dict[str, Any]]
    issues: List[dict]
    summary: dict
    error_count: int
    warning_count: int
    created_at: float = field(default_factory=time.time)


class _UploadStore:
    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._data: Dict[str, StoredUpload] = {}
        self._lock = threading.Lock()

    def _purge(self) -> None:
        now = time.time()
        expired = [k for k, v in self._data.items() if now - v.created_at > self._ttl]
        for k in expired:
            self._data.pop(k, None)

    def put(self, upload: StoredUpload) -> None:
        with self._lock:
            self._purge()
            self._data[upload.upload_id] = upload

    def get(self, upload_id: str) -> Optional[StoredUpload]:
        with self._lock:
            self._purge()
            return self._data.get(upload_id)

    def pop(self, upload_id: str) -> Optional[StoredUpload]:
        with self._lock:
            self._purge()
            return self._data.pop(upload_id, None)


upload_store = _UploadStore()


def new_upload_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Import (commit) — runs inside a single transaction
# ---------------------------------------------------------------------------


def import_upload(db, stored: StoredUpload) -> dict:
    """Persist a previously-validated upload. Caller wraps commit/rollback."""
    specs, _ = resolve_columns(db, stored.column_ids)
    spec_by_id = {s.id: s for s in specs}

    inserted = {"projects": 0, "sites": 0, "sampling_events": 0, "samples": 0, "measurements": 0, "new_variables": 0}
    skipped_rows = 0

    # Caches keyed by natural keys to keep the import idempotent.
    projects_cache: Dict[str, Project] = {}
    sites_cache: Dict[Tuple[int, str], Site] = {}
    events_cache: Dict[Tuple[int, int, Optional[date], Optional[str]], SamplingEvent] = {}
    samples_cache: Dict[Tuple[int, str], Sample] = {}
    sample_types_cache: Dict[str, SampleType] = {}

    for row in stored.rows:
        row_num = row["_row"]
        project_code = normalize_code(row.get("project_code"))
        site_code_raw = _clean_str(row.get("site_code"))
        sample_code = _clean_str(row.get("sample_code"))
        if not project_code or not site_code_raw or not sample_code:
            skipped_rows += 1
            continue

        # --- project ---
        project = _get_or_create_project(db, projects_cache, project_code, _clean_str(row.get("project_name")), row, inserted)
        if _clean_str(row.get("project_country")):
            project.country = project.country or _clean_str(row.get("project_country"))
        if _clean_str(row.get("project_donor")):
            project.donor = project.donor or _clean_str(row.get("project_donor"))

        # --- site ---
        lat, _, _ = parse_numeric(row.get("latitude"))
        lon, _, _ = parse_numeric(row.get("longitude"))
        site = _get_or_create_site(db, sites_cache, project, site_code_raw, lat, lon, row, inserted)

        # --- sampling event ---
        event_date, _ = parse_date(row.get("event_date"))
        activity = _clean_str(row.get("event_activity"))
        event = _get_or_create_event(db, events_cache, project, site, event_date, activity, row, inserted)

        # --- sample ---
        sample_type = _get_sample_type(db, sample_types_cache, normalize_code(row.get("sample_type_code")))
        sample = _get_or_create_sample(db, samples_cache, event, site, sample_type, sample_code, row, inserted)

        # --- EAV attributes & measurements ---
        for cid in stored.column_ids:
            spec = spec_by_id.get(cid)
            if spec is None or spec.kind == "structural":
                continue
            raw_value = row.get(cid)
            if _clean_str(raw_value) is None:
                continue
            if spec.kind == "attr":
                _upsert_attribute_value(db, sample, spec, raw_value, stored.filename, row_num)
            elif spec.kind == "var":
                created = _upsert_measurement(db, sample, spec, raw_value, stored.filename, row_num)
                if created:
                    inserted["measurements"] += 1

    db.flush()
    return {"inserted": inserted, "skipped_rows": skipped_rows}


def _trace_notes(source_file: str, row_num: int, original_value: Any) -> dict:
    return {"source_file": source_file, "row_number": row_num, "original_value": None if original_value is None else str(original_value)}


def _get_or_create_project(db, cache, code, name, row, inserted) -> Project:
    if code in cache:
        return cache[code]
    project = db.query(Project).filter(Project.code == code).first()
    if not project:
        project = Project(code=code, name=name or code, notes=_trace_notes(row.get("_source_file", ""), row["_row"], row.get("project_name")))
        db.add(project)
        db.flush()
        inserted["projects"] += 1
    cache[code] = project
    return project


def _get_or_create_site(db, cache, project: Project, site_code_raw, lat, lon, row, inserted) -> Site:
    site_code = normalize_code(site_code_raw) or site_code_raw
    key = (project.id, site_code)
    if key in cache:
        return cache[key]
    site = db.query(Site).filter(Site.project_id == project.id, Site.site_code == site_code).first()
    if not site:
        geom = None
        if lat is not None and lon is not None:
            geom = func.ST_SetSRID(func.ST_MakePoint(float(lon), float(lat)), 4326)
        site = Site(
            project_id=project.id,
            site_code=site_code,
            country=_clean_str(row.get("site_country")),
            admin_1=_clean_str(row.get("admin_1")),
            admin_2=_clean_str(row.get("admin_2")),
            admin_3=_clean_str(row.get("admin_3")),
            altitude_m=parse_numeric(row.get("altitude_m"))[0],
            geom=geom,
        )
        db.add(site)
        db.flush()
        inserted["sites"] += 1
    cache[key] = site
    return site


def _get_or_create_event(db, cache, project: Project, site: Site, event_date, activity, row, inserted) -> SamplingEvent:
    key = (project.id, site.id, event_date, activity)
    if key in cache:
        return cache[key]
    query = db.query(SamplingEvent).filter(
        SamplingEvent.project_id == project.id,
        SamplingEvent.site_id == site.id,
    )
    query = query.filter(SamplingEvent.event_date == event_date) if event_date is not None else query.filter(SamplingEvent.event_date.is_(None))
    query = query.filter(SamplingEvent.activity == activity) if activity is not None else query.filter(SamplingEvent.activity.is_(None))
    event = query.first()
    if not event:
        event = SamplingEvent(
            project_id=project.id,
            site_id=site.id,
            event_date=event_date,
            activity=activity,
            event_type=_clean_str(row.get("event_type")),
        )
        db.add(event)
        db.flush()
        inserted["sampling_events"] += 1
    cache[key] = event
    return event


def _get_sample_type(db, cache, code: Optional[str]) -> SampleType:
    code = code or "SOIL"
    if code in cache:
        return cache[code]
    sample_type = db.query(SampleType).filter(SampleType.sample_type_code == code).first()
    if not sample_type:
        # Catalog is meant to be controlled, but tolerate unseen codes on import.
        sample_type = SampleType(sample_type_code=code, name=code.replace("_", " ").title())
        db.add(sample_type)
        db.flush()
    cache[code] = sample_type
    return sample_type


def _get_or_create_sample(db, cache, event: SamplingEvent, site: Site, sample_type: SampleType, sample_code, row, inserted) -> Sample:
    key = (event.id, sample_code)
    if key in cache:
        return cache[key]
    sample = db.query(Sample).filter(Sample.event_id == event.id, Sample.sample_code == sample_code).first()
    if not sample:
        sample = Sample(
            event_id=event.id,
            site_id=site.id,
            sample_type_id=sample_type.id,
            sample_code=sample_code,
        )
        db.add(sample)
        db.flush()
        inserted["samples"] += 1
    cache[key] = sample
    return sample


def _assign_typed_value(target, value_type: str, raw_value: Any) -> Optional[str]:
    """Set the right value_* column on ``target``. Returns a qualifier if any."""
    qualifier = None
    if value_type == "numeric":
        value, qualifier, _ = parse_numeric(raw_value)
        target.value_numeric = value
    elif value_type == "date":
        target.value_date, _ = parse_date(raw_value)
    elif value_type == "boolean":
        target.value_boolean, _ = parse_boolean(raw_value)
    else:
        target.value_text = _clean_str(raw_value)
    return qualifier


def _upsert_measurement(db, sample: Sample, spec: ColumnSpec, raw_value: Any, source_file: str, row_num: int) -> bool:
    measurement = (
        db.query(Measurement)
        .filter(Measurement.sample_id == sample.id, Measurement.variable_id == spec.ref_id, Measurement.lab_batch_id.is_(None))
        .first()
    )
    created = False
    if not measurement:
        measurement = Measurement(sample_id=sample.id, variable_id=spec.ref_id)
        db.add(measurement)
        created = True
    # reset values before assigning (single-value check constraint)
    measurement.value_numeric = None
    measurement.value_text = None
    measurement.value_boolean = None
    measurement.value_date = None
    qualifier = _assign_typed_value(measurement, spec.value_type, raw_value)
    if qualifier:
        measurement.qualifier_result = qualifier
    measurement.notes = _trace_notes(source_file, row_num, raw_value)
    db.flush()
    return created


def _upsert_attribute_value(db, sample: Sample, spec: ColumnSpec, raw_value: Any, source_file: str, row_num: int) -> None:
    value = (
        db.query(SampleAttributeValue)
        .filter(
            SampleAttributeValue.sample_id == sample.id,
            SampleAttributeValue.sample_attribute_id == spec.ref_id,
            SampleAttributeValue.effective_date.is_(None),
        )
        .first()
    )
    if not value:
        value = SampleAttributeValue(sample_id=sample.id, sample_attribute_id=spec.ref_id)
        db.add(value)
    value.value_numeric = None
    value.value_text = None
    value.value_boolean = None
    value.value_date = None
    _assign_typed_value(value, spec.value_type, raw_value)
    value.notes = _trace_notes(source_file, row_num, raw_value)
    db.flush()
