"""
Shared ETL engine for the bulk upload flow (carga de datos).

The same parser/validator is used by both ``POST /upload/validate`` (dry-run) and
``POST /upload/import`` (commit) so that "what was validated" is byte-for-byte
"what gets imported", as required by the data dictionary
(``DATA_DICTIONARY_soil_platform_v6.md``).

Column id conventions used across the template, validation and import:

- ``var:<variable_id>``      -> a measurement over a catalog variable.
- ``attr:<ATTRIBUTE_CODE>``  -> an EAV attribute of the sample.
- ``newvar:<VARIABLE_CODE>`` -> a measurement over a variable the uploader
                                defined in the template's "New variables" sheet.
                                It is not in the catalog until a reviewer approves.
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
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import func

from mflsoildata_orm.enums import Matrix, ValueType
from mflsoildata_orm.models import (
    Measurement,
    Project,
    Sample,
    SampleAttribute,
    SampleAttributeValue,
    SampleType,
    SamplingEvent,
    Site,
    Unit,
    Variable,
)
from schemas.upload import ColumnMappingEntry

# ---------------------------------------------------------------------------
# Constants & catalog of structural columns
# ---------------------------------------------------------------------------

# Default (primary) language for API-facing labels. Pass ``lang="es"`` to localize.
LANG_DEFAULT = "en"


def _t(translations: Dict[str, str], lang: str) -> str:
    """Pick a translation, falling back to the default language."""
    return translations.get(lang) or translations.get(LANG_DEFAULT) or ""


TEMPLATE_SHEET = "Template"
# Human-readable glossary/metadata sheet. Excluded from parsing by name, so it can
# be a rich, visible reference for whoever fills the template.
META_SHEET = "Glossary"
# Both language variants are recognized when RE-parsing an uploaded file, so
# templates downloaded in either language round-trip correctly.
META_SHEET_NAMES = {"Glossary", "Glosario"}
TEMPLATE_SHEET_NAMES = {"Template", "Plantilla"}
# Where an uploader describes variables that are not in the catalog yet, and the
# (hidden) sheet that feeds that sheet's dropdowns. Neither is ever parsed as data.
NEW_VARIABLES_SHEET = {"en": "New variables", "es": "Variables nuevas"}
NEW_VARIABLE_SHEET_NAMES = set(NEW_VARIABLES_SHEET.values())
LISTS_SHEET = "Lists"
NON_DATA_SHEET_NAMES = META_SHEET_NAMES | NEW_VARIABLE_SHEET_NAMES | {LISTS_SHEET, "Listas"}

VALID_VALUE_TYPES = {item.value for item in ValueType}
VALID_MATRICES = {item.value for item in Matrix}

# Friendly labels for value types (used in the glossary sheet), per language.
VALUE_TYPE_LABELS = {
    "en": {"numeric": "Number", "text": "Text", "date": "Date", "boolean": "Yes/No"},
    "es": {"numeric": "Número", "text": "Texto", "date": "Fecha", "boolean": "Sí/No"},
}

# Glossary sheet column headers, per language.
GLOSSARY_HEADERS = {
    "en": ["Column", "Technical field", "Target (table.field)", "Type", "Required", "Unit", "Description", "Example"],
    "es": ["Columna", "Campo técnico", "Destino (tabla.campo)", "Tipo", "Obligatorio", "Unidad", "Descripción", "Ejemplo"],
}
YES_NO = {"en": ("Yes", "No"), "es": ("Sí", "No")}
TEMPLATE_FILENAME = {"en": "upload_template", "es": "plantilla_carga"}

# Template picker group labels/descriptions, per language.
GROUP_LABELS = {
    "estructura": {"en": "Identification (required)", "es": "Identificación (obligatorio)"},
    "atributos_muestra": {"en": "Sample attributes (optional)", "es": "Atributos de muestra (opcional)"},
    "variables_none": {"en": "Variables (no matrix)", "es": "Variables (sin matriz)"},
}
GROUP_DESCRIPTIONS = {
    "estructura": {
        "en": "Minimum fields to locate the sample by project, site, event, and sample.",
        "es": "Campos mínimos para ubicar la muestra en proyecto, sitio, evento y muestra.",
    },
    "atributos_muestra": {
        "en": "Stored as EAV in sample_attribute_values.",
        "es": "Se guardan como EAV en sample_attribute_values.",
    },
    "variables": {
        "en": "Each selected column generates measurements for that variable.",
        "es": "Cada columna seleccionada genera measurements para esa variable.",
    },
}

# Preferred order for grouping variables by matrix in the template picker.
MATRIX_ORDER = ["SOIL", "LEAF", "GRAIN", "SOIL_PROFILE", "SOIL_HORIZON"]
MATRIX_GROUP_LABELS = {
    "en": {
        "SOIL": "Variables — Soil (SOIL)",
        "LEAF": "Variables — Leaf (LEAF)",
        "GRAIN": "Variables — Grain (GRAIN)",
        "SOIL_PROFILE": "Variables — Profile (SOIL_PROFILE)",
        "SOIL_HORIZON": "Variables — Horizon (SOIL_HORIZON)",
    },
    "es": {
        "SOIL": "Variables — Suelo (SOIL)",
        "LEAF": "Variables — Hoja (LEAF)",
        "GRAIN": "Variables — Grano (GRAIN)",
        "SOIL_PROFILE": "Variables — Perfil (SOIL_PROFILE)",
        "SOIL_HORIZON": "Variables — Horizonte (SOIL_HORIZON)",
    },
}


def matrix_group_label(key: str, lang: str = LANG_DEFAULT) -> str:
    """Localized label for a variables-by-matrix group ('' means no matrix)."""
    table = MATRIX_GROUP_LABELS.get(lang, MATRIX_GROUP_LABELS[LANG_DEFAULT])
    if key in table:
        return table[key]
    if key == "":
        return _t(GROUP_LABELS["variables_none"], lang)
    return f"Variables — {key}"

# Tokens that mean "no value".
MISSING_TOKENS = {"", "NA", "N/A", "-", "--", "NULL", "NONE", "."}

# Qualifiers recognised in measurement cells (data dictionary 2.3).
QUALIFIER_RE = re.compile(r"^\s*(<|>)\s*(.+)$")

# Detection-limit tokens written instead of a number: "< LOQ", "<LOD", "BDL".
# They carry real analytical meaning (the analyte was below the limit), so they
# are stored as a qualifier with a null value rather than rejected as garbage.
LIMIT_TOKENS = {"LOQ", "LOD", "DL", "MDL", "BDL", "BLD"}

# --- "New variables" sheet (variables the uploader proposes) ---------------------

# Header of each column of that sheet, per language, in the order they appear.
NEWVAR_HEADERS = {
    "en": ["Variable name", "Value type", "Matrix", "Unit", "Domain", "Description"],
    "es": ["Nombre de la variable", "Tipo de valor", "Matriz", "Unidad", "Dominio", "Descripción"],
}
# Header text (folded, see _fold) -> field of the definition. Both languages and
# the short forms are accepted so a hand-edited sheet still parses.
_NEWVAR_HEADER_FIELDS = {
    "variable name": "name", "nombre de la variable": "name", "name": "name", "nombre": "name",
    "value type": "value_type", "tipo de valor": "value_type", "type": "value_type", "tipo": "value_type",
    "matrix": "matrix", "matriz": "matrix",
    "unit": "unit", "unidad": "unit",
    "domain": "domain", "dominio": "domain",
    "description": "description", "descripcion": "description",
}
_VALUE_TYPE_ALIASES = {
    "numeric": "numeric", "number": "numeric", "numero": "numeric", "numerico": "numeric",
    "text": "text", "texto": "text",
    "boolean": "boolean", "booleano": "boolean", "yes no": "boolean", "si no": "boolean",
    "date": "date", "fecha": "date",
}
_MATRIX_ALIASES = {
    "soil": "SOIL", "suelo": "SOIL",
    "leaf": "LEAF", "hoja": "LEAF",
    "grain": "GRAIN", "grano": "GRAIN",
    "soil profile": "SOIL_PROFILE", "profile": "SOIL_PROFILE", "perfil": "SOIL_PROFILE", "perfil de suelo": "SOIL_PROFILE",
    "soil horizon": "SOIL_HORIZON", "horizon": "SOIL_HORIZON", "horizonte": "SOIL_HORIZON",
}
# Text of the matrix dropdown, per language ("Suelo (SOIL)").
MATRIX_CHOICE_LABELS = {
    "en": {"SOIL": "Soil (SOIL)", "LEAF": "Leaf (LEAF)", "GRAIN": "Grain (GRAIN)",
           "SOIL_PROFILE": "Soil profile (SOIL_PROFILE)", "SOIL_HORIZON": "Soil horizon (SOIL_HORIZON)"},
    "es": {"SOIL": "Suelo (SOIL)", "LEAF": "Hoja (LEAF)", "GRAIN": "Grano (GRAIN)",
           "SOIL_PROFILE": "Perfil de suelo (SOIL_PROFILE)", "SOIL_HORIZON": "Horizonte de suelo (SOIL_HORIZON)"},
}
NEWVAR_INSTRUCTIONS = {
    "en": [
        "Variables that are not in the catalogue yet",
        "1) In the 'Template' sheet, add a new column at the right and write the variable's name as its header (row 1). Leave row 2 (hidden) empty.",
        "2) Describe that same variable below, one row per variable. The name must match the column header exactly.",
        "3) A reviewer checks each new variable before it is added to the catalogue, and may adjust it or merge it with an existing one.",
        "Domain (optional) is a general category used to group variables, e.g. LAB_RESULT for laboratory results. Pick one from the list or leave it empty if unsure: it is saved as CUSTOM.",
    ],
    "es": [
        "Variables que aún no están en el catálogo",
        "1) En la hoja 'Template', agrega una columna nueva a la derecha y escribe el nombre de la variable como encabezado (fila 1). Deja vacía la fila 2 (oculta).",
        "2) Describe esa misma variable aquí abajo, una fila por variable. El nombre debe coincidir exactamente con el encabezado de la columna.",
        "3) Una persona revisora comprueba cada variable nueva antes de agregarla al catálogo y puede ajustarla o fusionarla con una existente.",
        "Dominio (opcional) es una categoría general para agrupar variables, p. ej. LAB_RESULT para resultados de laboratorio. Elige una de la lista o déjalo vacío si no estás seguro: se guarda como CUSTOM.",
    ],
}
NEWVAR_MAX_ROWS = 200  # rows the sheet's dropdowns cover
DEFAULT_VARIABLE_DOMAIN = "CUSTOM"


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
    StructuralColumn("project_code", "Project Code", True, "text", "projects.code", "CLOC-CO"),
    StructuralColumn("project_name", "Project Name", True, "text", "projects.name", "Clima-LoCa Colombia"),
    StructuralColumn("site_code", "Site Code", True, "text", "sites.site_code", "S-001"),
    StructuralColumn("latitude", "Site Latitude", True, "numeric", "sites.geom", "4.6097"),
    StructuralColumn("longitude", "Site Longitude", True, "numeric", "sites.geom", "-74.0817"),
    StructuralColumn("event_date", "Sampling Date", True, "date", "sampling_events.event_date", "2024-04-15"),
    StructuralColumn("sample_code", "Sample Code", True, "text", "samples.sample_code", "M-001"),
    StructuralColumn("sample_type_code", "Sample Type", True, "text", "samples.sample_type_id", "SOIL"),
    # Optional structural columns
    StructuralColumn("project_country", "Project Country", False, "text", "projects.country", "Colombia"),
    StructuralColumn("project_donor", "Donor", False, "text", "projects.donor"),
    StructuralColumn("site_country", "Site Country", False, "text", "sites.country", "Colombia"),
    StructuralColumn("admin_1", "Admin 1 (Department/State)", False, "text", "sites.admin_1"),
    StructuralColumn("admin_2", "Admin 2 (Municipality)", False, "text", "sites.admin_2"),
    StructuralColumn("admin_3", "Admin 3", False, "text", "sites.admin_3"),
    StructuralColumn("altitude_m", "Altitude (m)", False, "numeric", "sites.altitude_m", unit="m"),
    StructuralColumn("event_activity", "Event Activity", False, "text", "sampling_events.activity"),
    StructuralColumn("event_type", "Event Type", False, "text", "sampling_events.event_type"),
]

STRUCTURAL_BY_ID: Dict[str, StructuralColumn] = {c.id: c for c in STRUCTURAL_COLUMNS}
REQUIRED_STRUCTURAL_IDS = [c.id for c in STRUCTURAL_COLUMNS if c.required]

# Spanish overrides for the structural column labels (English lives on the dataclass).
STRUCTURAL_LABELS_ES: Dict[str, str] = {
    "project_code": "Código de Proyecto",
    "project_name": "Nombre del Proyecto",
    "site_code": "Código del Sitio",
    "latitude": "Latitud del Sitio",
    "longitude": "Longitud del Sitio",
    "event_date": "Fecha de Muestreo",
    "sample_code": "Código de Muestra",
    "sample_type_code": "Tipo de Muestra",
    "project_country": "País del Proyecto",
    "project_donor": "Donante",
    "site_country": "País del Sitio",
    "admin_1": "Admin 1 (Departamento)",
    "admin_2": "Admin 2 (Municipio)",
    "admin_3": "Admin 3",
    "altitude_m": "Altitud (m)",
    "event_activity": "Actividad del Evento",
    "event_type": "Tipo de Evento",
}

# Human descriptions for structural columns (used in the glossary sheet). English
# is the primary language; Spanish overrides live in STRUCTURAL_DESCRIPTIONS_ES.
STRUCTURAL_DESCRIPTIONS: Dict[str, str] = {
    "project_code": "Unique, stable project code (uppercase, no accents).",
    "project_name": "Human-readable project name.",
    "site_code": "Site code, unique within the project.",
    "latitude": "Site latitude in decimal degrees (WGS84).",
    "longitude": "Site longitude in decimal degrees (WGS84).",
    "event_date": "Sampling or visit date (YYYY-MM-DD).",
    "sample_code": "External sample identifier, unique per event.",
    "sample_type_code": "Sample type: SOIL, LEAF, GRAIN, SOIL_PROFILE or SOIL_HORIZON.",
    "project_country": "Main country of the project.",
    "project_donor": "Project donor or funder.",
    "site_country": "Country of the site.",
    "admin_1": "Administrative division level 1 (e.g. department/province/state).",
    "admin_2": "Administrative division level 2 (e.g. municipality).",
    "admin_3": "Administrative division level 3 (e.g. village/parish).",
    "altitude_m": "Site altitude in meters above sea level.",
    "event_activity": "Activity carried out during the sampling event.",
    "event_type": "Event or campaign type.",
}
STRUCTURAL_DESCRIPTIONS_ES: Dict[str, str] = {
    "project_code": "Código único y estable del proyecto (mayúsculas, sin tildes).",
    "project_name": "Nombre legible del proyecto.",
    "site_code": "Código del sitio, único dentro del proyecto.",
    "latitude": "Latitud del sitio en grados decimales (WGS84).",
    "longitude": "Longitud del sitio en grados decimales (WGS84).",
    "event_date": "Fecha del muestreo o visita (AAAA-MM-DD).",
    "sample_code": "Identificador externo de la muestra, único por evento.",
    "sample_type_code": "Tipo de muestra: SOIL, LEAF, GRAIN, SOIL_PROFILE o SOIL_HORIZON.",
    "project_country": "País principal del proyecto.",
    "project_donor": "Donante o financiador del proyecto.",
    "site_country": "País del sitio.",
    "admin_1": "División administrativa nivel 1 (p. ej. departamento/provincia).",
    "admin_2": "División administrativa nivel 2 (p. ej. municipio).",
    "admin_3": "División administrativa nivel 3 (p. ej. vereda/corregimiento).",
    "altitude_m": "Altitud del sitio en metros sobre el nivel del mar.",
    "event_activity": "Actividad realizada en el evento de muestreo.",
    "event_type": "Tipo de evento o campaña.",
}


def structural_label(cid: str, lang: str = LANG_DEFAULT) -> str:
    """Localized display label for a structural column id."""
    sc = STRUCTURAL_BY_ID.get(cid)
    if sc is None:
        return cid
    if lang == "es":
        return STRUCTURAL_LABELS_ES.get(cid, sc.label)
    return sc.label


def structural_description(cid: str, lang: str = LANG_DEFAULT) -> str:
    """Localized glossary description for a structural column id."""
    if lang == "es":
        return STRUCTURAL_DESCRIPTIONS_ES.get(cid, STRUCTURAL_DESCRIPTIONS.get(cid, ""))
    return STRUCTURAL_DESCRIPTIONS.get(cid, "")


def variable_display_label(name: str, matrix: Optional[str]) -> str:
    """Disambiguate variables that share a name across matrices (e.g. Zn Total en SOIL/LEAF/GRAIN)."""
    return f"{name} — {matrix}" if matrix else name


def is_known_id(token: str) -> bool:
    """Does ``token`` look like a technical column id (used for header detection)?"""
    if not token:
        return False
    token = token.strip()
    return (
        token in STRUCTURAL_BY_ID
        or token.startswith("var:")
        or token.startswith("attr:")
        or token.startswith("newvar:")
    )


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



# Validation messages say how to fix the cell, not just what is wrong: the person
# fixing the file is a field scientist, not the author of this parser.
DATE_HELP = (
    "Invalid date. Use a specific date formatted as YYYY-MM-DD (e.g. 2019-03-15); "
    "DD/MM/YYYY and DD-MM-YYYY also work. A range such as 'March/April 2019' is not "
    "a date — use the sampling day for this row."
)
NUMBER_HELP = (
    "Not a number. Enter the number only (e.g. 12.5), with a dot or comma decimal "
    "separator. For results below the detection limit write '<LOQ', '<LOD' or "
    "'<0.5'. If there is no value, leave the cell empty or write 'NA'."
)

def parse_numeric(raw: Any) -> Tuple[Optional[Decimal], Optional[str], bool]:
    """Return (value, qualifier, ok). ``ok`` False means the token was unparseable."""
    if raw is None:
        return None, None, True
    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw)), None, True
    text = str(raw).strip()
    if text == "" or text.upper() in MISSING_TOKENS:
        return None, None, True

    # "ND" / "n.d." = not detected. A result, not a missing value, so it is kept
    # as a qualifier. (The old check for this sat inside the MISSING_TOKENS branch,
    # which "ND" never matches, so it was dead code and every ND cell was rejected.)
    if text.upper().replace(".", "").replace(" ", "") == "ND":
        return None, "ND", True

    qualifier: Optional[str] = None
    match = QUALIFIER_RE.match(text)
    if match:
        qualifier = match.group(1)
        text = match.group(2).strip()

    # "< LOQ" / "BDL": below the detection limit. No number to store, but the
    # qualifier is the result, so keep it and report the cell as valid.
    bare = text.upper().replace(" ", "").replace("-", "").replace("_", "")
    if bare in LIMIT_TOKENS:
        return None, f"{qualifier or '<'}{bare}", True

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
    description: Optional[str] = None
    # Set for a ``newvar:`` column: the uploader's definition. ``ref_id`` stays
    # None until import creates (or merges into) the real variable.
    new_def: Optional[dict] = None


def resolve_columns(
    db, ids: List[str], lang: str = LANG_DEFAULT, new_variables: Optional[List[dict]] = None
) -> Tuple[List[ColumnSpec], List[dict]]:
    """Resolve raw column ids to specs, reporting unknown variables/attributes.

    ``new_variables`` are the uploader's definitions (see ``process_new_variables``)
    that ``newvar:<code>`` columns refer to.
    """
    issues: List[dict] = []
    specs: List[ColumnSpec] = []
    new_defs = {d["code"]: d for d in (new_variables or [])}

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
                    id=cid, kind="structural", label=structural_label(cid, lang), value_type=sc.value_type,
                    required=sc.required, code=cid, unit=sc.unit, db_target=sc.db_target,
                    description=structural_description(cid, lang),
                )
            )
        elif cid.startswith("var:"):
            raw = cid.split(":", 1)[1]
            try:
                vid = int(raw)
            except ValueError:
                issues.append({"row": 0, "column": cid, "value": cid, "message": "Invalid variable identifier", "severity": "error", "code": "UNKNOWN_COLUMN"})
                continue
            variable = variables.get(vid)
            if not variable:
                issues.append({"row": 0, "column": cid, "value": cid, "message": f"Variable {vid} does not exist in the catalog", "severity": "error", "code": "UNKNOWN_VARIABLE"})
                continue
            if variable.description:
                var_desc = variable.description
            elif lang == "es":
                var_desc = f"Variable de {variable.domain}" + (f", matriz {variable.matrix}" if variable.matrix else "")
            else:
                var_desc = f"{variable.domain} variable" + (f", {variable.matrix} matrix" if variable.matrix else "")
            specs.append(
                ColumnSpec(
                    id=cid, kind="var",
                    label=variable_display_label(variable.name, variable.matrix),
                    value_type=variable.value_type,
                    code=variable.variable_code, ref_id=variable.id, db_target="measurements",
                    description=var_desc,
                )
            )
        elif cid.startswith("newvar:"):
            new_def = new_defs.get(cid.split(":", 1)[1])
            if not new_def:
                issues.append({"row": 0, "column": cid, "value": cid, "message": "New variable is not defined in the 'New variables' sheet", "severity": "error", "code": "UNKNOWN_VARIABLE"})
                continue
            specs.append(
                ColumnSpec(
                    id=cid, kind="var", label=variable_display_label(new_def["name"], new_def.get("matrix")),
                    value_type=new_def["value_type"], code=new_def["code"], ref_id=None,
                    db_target="measurements (new variable)", unit=new_def.get("unit"),
                    description=new_def.get("description") or "New variable, pending review",
                    new_def=new_def,
                )
            )
        elif cid.startswith("attr:"):
            code = cid.split(":", 1)[1]
            attribute = attributes.get(code)
            if not attribute:
                issues.append({"row": 0, "column": cid, "value": cid, "message": f"Attribute {code} does not exist in the catalog", "severity": "error", "code": "UNKNOWN_ATTRIBUTE"})
                continue
            if attribute.description:
                attr_desc = attribute.description
            elif lang == "es":
                attr_desc = "Atributo dinámico de muestra" + (f" (categoría {attribute.category})" if attribute.category else "")
            else:
                attr_desc = "Dynamic sample attribute" + (f" (category {attribute.category})" if attribute.category else "")
            specs.append(
                ColumnSpec(
                    id=cid, kind="attr", label=attribute.name, value_type=attribute.value_type,
                    code=attribute.sample_attribute_code, ref_id=attribute.id, db_target="sample_attribute_values",
                    description=attr_desc,
                )
            )
        else:
            issues.append({"row": 0, "column": cid, "value": cid, "message": "Unrecognized column", "severity": "warning", "code": "UNKNOWN_COLUMN"})

    return specs, issues


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------


@dataclass
class ParsedFile:
    column_ids: List[str]
    rows: List[Dict[str, Any]]  # each row: {column_id: raw_value, "_row": int}
    # From the "New variables" sheet: normalized definitions (see
    # ``read_new_variable_defs``), the problems found in them, and the headers of
    # columns that carry no technical id and no definition (so nothing reads them).
    new_variable_defs: List[Dict[str, Any]] = field(default_factory=list)
    new_variable_issues: List[Dict[str, Any]] = field(default_factory=list)
    ignored_columns: List[str] = field(default_factory=list)


def _fold(text: Any) -> str:
    """Lowercase, accent-free, punctuation-free form of ``text`` for lenient matching."""
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def derive_variable_code(name: str, matrix: Optional[str]) -> Optional[str]:
    """Catalog code for a variable: ``<MATRIX>_<NAME>`` (``SOIL_AL_DISP_MG_KG``)."""
    return normalize_code(f"{matrix}_{name}" if matrix else name)


def _newvar_issue(
    row: int, name: Optional[str], value: Any, message: str, severity: str, code: str, sheet: Optional[str] = None
) -> dict:
    """``row`` is a row of ``sheet`` when a sheet is given (the "New variables" sheet,
    not the data sheet); 0 means the problem is about the file/column, not a row."""
    return {"row": row, "column": name, "value": None if value is None else str(value), "message": message, "severity": severity, "code": code, "sheet": sheet}


def read_new_variable_defs(content: bytes, filename: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Read the template's "New variables" sheet into normalized definitions.

    Returns ``(defs, issues)``. Only .xlsx can carry the sheet. Each definition is
    ``{code, name, value_type, matrix, unit, description, source_row}``; ``unit`` is
    still the text the uploader typed (it is resolved against the catalog later).
    Rows with errors are reported and left out.
    """
    if not (filename or "").lower().endswith((".xlsx", ".xlsm")):
        return [], []
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception:
        return [], []
    sheet_name = next((s for s in wb.sheetnames if s in NEW_VARIABLE_SHEET_NAMES), None)
    if sheet_name is None:
        return [], []

    rows = [list(r) for r in wb[sheet_name].iter_rows(values_only=True)]
    header_idx, columns = None, {}
    for idx, row in enumerate(rows[:15]):
        found = {
            _NEWVAR_HEADER_FIELDS[_fold(c)]: i
            for i, c in enumerate(row)
            if c not in (None, "") and _fold(c) in _NEWVAR_HEADER_FIELDS
        }
        if "name" in found:
            header_idx, columns = idx, found
            break
    if header_idx is None:
        return [], []

    def cell(row: List[Any], field_name: str) -> Optional[str]:
        i = columns.get(field_name)
        return _clean_str(row[i]) if i is not None and i < len(row) else None

    defs: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    seen_names: Dict[str, int] = {}
    seen_codes: Dict[str, str] = {}
    for offset, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        name = cell(row, "name")
        if not name:
            continue
        sheet_row = offset

        raw_type = cell(row, "value_type")
        value_type = _VALUE_TYPE_ALIASES.get(_fold(raw_type), None) if raw_type else "numeric"
        if value_type is None:
            issues.append(_newvar_issue(sheet_row, name, raw_type, f"Unknown value type '{raw_type}'. Use Number, Text, Date or Yes/No.", "error", "NEW_VARIABLE_INVALID_TYPE", sheet_name))
            continue

        raw_matrix = cell(row, "matrix")
        matrix = None
        if raw_matrix:
            in_parens = re.search(r"\(([A-Za-z_]+)\)", raw_matrix)
            candidates = [in_parens.group(1).upper()] if in_parens else []
            candidates += [_MATRIX_ALIASES.get(_fold(raw_matrix)), raw_matrix.strip().upper().replace(" ", "_")]
            matrix = next((c for c in candidates if c in VALID_MATRICES), None)
            if matrix is None:
                issues.append(_newvar_issue(sheet_row, name, raw_matrix, f"Unknown matrix '{raw_matrix}'. Use one of: {', '.join(sorted(VALID_MATRICES))}.", "error", "NEW_VARIABLE_INVALID_MATRIX", sheet_name))
                continue

        name_key = normalize_code(name)
        if name_key in seen_names:
            issues.append(_newvar_issue(sheet_row, name, name, f"'{name}' is described twice in this sheet (row {seen_names[name_key]}). Each name must be unique, because it is how the column is found.", "error", "NEW_VARIABLE_DUPLICATE", sheet_name))
            continue
        seen_names[name_key] = sheet_row

        code = derive_variable_code(name, matrix)
        if not code:
            issues.append(_newvar_issue(sheet_row, name, name, "The variable name has no usable letters or digits.", "error", "NEW_VARIABLE_INVALID_NAME", sheet_name))
            continue
        if code in seen_codes:
            issues.append(_newvar_issue(sheet_row, name, name, f"'{name}' produces the same code as '{seen_codes[code]}'. Make the names more distinct.", "error", "NEW_VARIABLE_DUPLICATE", sheet_name))
            continue
        seen_codes[code] = name

        defs.append({
            "code": code,
            "name": name,
            "value_type": value_type,
            "matrix": matrix,
            "unit": cell(row, "unit"),
            "domain": normalize_code(cell(row, "domain")) or DEFAULT_VARIABLE_DOMAIN,
            "description": cell(row, "description"),
            "source_row": sheet_row,
            "source_sheet": sheet_name,
        })
    return defs, issues


def _assign_new_variable_columns(
    column_ids: List[str], label_row: List[Any], defs: List[Dict[str, Any]]
) -> Tuple[List[str], List[str]]:
    """Give ``newvar:<code>`` ids to template columns the uploader added by hand.

    Such a column has a header in the visible row and nothing in the hidden
    technical-id row; it is matched to its definition by name. Headers that match
    no definition are returned so they can be reported instead of silently dropped.
    """
    by_name = {normalize_code(d["name"]): d for d in defs}
    out, ignored = list(column_ids), []
    # A column added after the last one may lie beyond the id row's width.
    out += [""] * (len(label_row) - len(out))
    for i, cid in enumerate(out):
        if cid:
            continue
        label = _clean_str(label_row[i]) if i < len(label_row) else None
        if not label:
            continue
        found = by_name.get(normalize_code(label))
        if found:
            out[i] = f"newvar:{found['code']}"
        else:
            ignored.append(label)
    return out, ignored


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


def _header_index(all_rows: List[List[Any]], header_row: Optional[int]) -> int:
    """0-based index of the header row within *all_rows* for an explicit ``header_row``.

    ``header_row`` is 1-based (row 1 = the file's very first row, blanks included) —
    the same convention a spreadsheet UI (and the column-mapping preview) uses, so a
    frontend-picked row number lines up exactly with what gets parsed here.
    """
    idx = (header_row or 1) - 1
    if idx < 0 or idx >= len(all_rows):
        raise ValueError(
            f"header_row ({header_row or 1}) is out of range for a file with {len(all_rows)} row(s)."
        )
    return idx


def _column_ids_from_mapping(mapping: List[ColumnMappingEntry], row_width: int) -> List[str]:
    """Build a positional ``column_ids`` list from an explicit column mapping.

    Validates that every ``source_index`` fits the file's actual header width and
    that no two entries target the same destination column.
    """
    column_ids = [""] * row_width
    seen_targets: Dict[str, int] = {}
    for entry in mapping:
        if entry.source_index < 0 or entry.source_index >= row_width:
            raise ValueError(
                f"column_mapping references column index {entry.source_index}, "
                f"but the header row only has {row_width} column(s)."
            )
        target = (entry.target_id or "").strip()
        if not target:
            continue
        if target in seen_targets:
            raise ValueError(
                f"column '{target}' is mapped from two different source columns "
                f"({seen_targets[target]} and {entry.source_index}); each destination "
                "can only be used once."
            )
        seen_targets[target] = entry.source_index
        column_ids[entry.source_index] = target
    if not seen_targets:
        raise ValueError("column_mapping must map at least one column to a destination field.")
    return column_ids


def parse_file(
    content: bytes,
    filename: str,
    columns_hint: Optional[List[str]] = None,
    column_mapping: Optional[List[ColumnMappingEntry]] = None,
    header_row: Optional[int] = None,
) -> ParsedFile:
    """Parse an uploaded xlsx/csv file into ordered column ids + raw rows.

    Column-identity resolution, in priority order:

    1. ``column_mapping`` (explicit, from the column-mapping UI) — authoritative;
       never falls back to auto-detecting a hidden id row, so a user's own header
       row that coincidentally looks like one can never hijack an explicit mapping.
    2. A hidden/self-describing technical-id row, if found (how our own generated
       template works) — ``columns_hint``/``header_row`` are irrelevant here.
    3. ``columns_hint`` (positional fallback, the "ya tengo la plantilla" shortcut).
       ``header_row`` may be sent alongside it for files with extra rows above the
       header; without it, behavior is unchanged (a single assumed header row).
    4. Otherwise: error.
    """
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        all_rows = _read_csv(content)
    else:
        all_rows = _read_xlsx(content)

    if not any(any(c not in (None, "") for c in r) for r in all_rows):
        raise ValueError("The file contains no rows.")

    new_defs: List[Dict[str, Any]] = []
    new_issues: List[Dict[str, Any]] = []
    ignored_columns: List[str] = []

    if column_mapping:
        header_idx = _header_index(all_rows, header_row)
        column_ids = _column_ids_from_mapping(column_mapping, len(all_rows[header_idx]))
        data_start = header_idx + 1
        source_rows = all_rows
    else:
        raw_rows = [r for r in all_rows if any(c not in (None, "") for c in r)]
        if not raw_rows:
            raise ValueError("The file contains no rows.")

        id_row_idx = _detect_id_row(raw_rows)
        if id_row_idx >= 0:
            column_ids = [str(c).strip() if c is not None else "" for c in raw_rows[id_row_idx]]
            data_start = id_row_idx + 1
            source_rows = raw_rows
            # Columns added by hand to our own template: no technical id, matched
            # to the "New variables" sheet by header. (Only .xlsx carries that sheet.)
            new_defs, new_issues = read_new_variable_defs(content, filename)
            if id_row_idx >= 1:
                column_ids, ignored_columns = _assign_new_variable_columns(column_ids, raw_rows[id_row_idx - 1], new_defs)
        elif columns_hint:
            column_ids = [c.strip() for c in columns_hint]
            if header_row is not None:
                header_idx = _header_index(all_rows, header_row)
                data_start = header_idx + 1
                source_rows = all_rows
            else:
                data_start = 1  # legacy: assume a single human-readable header row
                source_rows = raw_rows
        else:
            raise ValueError(
                "Could not identify the columns. Use the template generated by /upload/template "
                "or send the 'columns' parameter."
            )

    rows: List[Dict[str, Any]] = []
    for offset, raw in enumerate(source_rows[data_start:], start=data_start):
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

    return ParsedFile(
        column_ids=[c for c in column_ids if c],
        rows=rows,
        new_variable_defs=new_defs,
        new_variable_issues=new_issues,
        ignored_columns=ignored_columns,
    )


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
    # Exclude the glossary / new-variables / lists sheets (either language) so only
    # the data sheet is parsed.
    data_sheets = [s for s in wb.sheetnames if s not in NON_DATA_SHEET_NAMES]
    preferred = next((s for s in wb.sheetnames if s in TEMPLATE_SHEET_NAMES), None)
    ws = wb[preferred] if preferred else wb[data_sheets[0] if data_sheets else wb.sheetnames[0]]
    return [list(r) for r in ws.iter_rows(values_only=True)]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _value_issue(row_num: int, column: str, value: Any, message: str, severity: str, code: str) -> dict:
    return {"row": row_num, "column": column, "value": None if value is None else str(value), "message": message, "severity": severity, "code": code}


# --- New variables defined by the uploader ----------------------------------------

SIMILAR_NAME_THRESHOLD = 0.85


def _name_signature(name: Optional[str]) -> str:
    """Folded name without any parenthesised unit/qualifier — what two spellings of
    the same analyte ("Al Disp (mg/kg)" / "Al disp") have in common."""
    return _fold(re.sub(r"\([^)]*\)", " ", name or ""))


def process_new_variables(db, parsed: ParsedFile) -> Tuple[List[dict], List[dict]]:
    """Check the uploader's new-variable definitions against the catalog.

    Returns ``(definitions to stage, issues)`` and may rewrite ``parsed`` in place:

    - a definition whose code already exists is *linked*: its column becomes the
      existing ``var:<id>`` (same analyte, same matrix — nothing to review);
    - one that resembles an existing variable keeps going, flagged
      ``similar_to`` so the reviewer can merge it instead of creating a duplicate;
    - definitions with no column are dropped; columns with no definition and no id
      are reported (they would otherwise be ignored without a word).
    """
    issues: List[dict] = list(parsed.new_variable_issues)
    for label in parsed.ignored_columns:
        issues.append(_newvar_issue(0, label, None, f"Column '{label}' has no technical id and is not described in the 'New variables' sheet, so it was ignored.", "warning", "UNDEFINED_COLUMN"))

    used = {cid.split(":", 1)[1] for cid in parsed.column_ids if cid.startswith("newvar:")}
    for d in parsed.new_variable_defs:
        if d["code"] not in used:
            issues.append(_newvar_issue(d["source_row"], d["name"], None, f"'{d['name']}' is described in the 'New variables' sheet but no column in the 'Template' sheet has that header.", "warning", "NEW_VARIABLE_UNUSED", d.get("source_sheet")))
    defs = [d for d in parsed.new_variable_defs if d["code"] in used]
    if not defs:
        return [], issues

    catalogue = db.query(Variable).all()
    by_code = {v.variable_code: v for v in catalogue}
    unit_by_key: Dict[str, Any] = {}
    for unit in db.query(Unit).all():
        for key in (unit.symbol, unit.unit_code):
            if key:
                unit_by_key.setdefault(key.strip().lower(), unit)

    staged: List[dict] = []
    links: Dict[str, int] = {}
    for d in defs:
        existing = by_code.get(d["code"])
        if existing:
            links[d["code"]] = existing.id
            issues.append(_newvar_issue(d["source_row"], d["name"], variable_display_label(existing.name, existing.matrix), f"'{d['name']}' already exists in the catalogue; the column was linked to it.", "warning", "NEW_VARIABLE_LINKED", d.get("source_sheet")))
            continue

        d["unit_id"] = None
        if d.get("unit"):
            unit = unit_by_key.get(d["unit"].strip().lower())
            if unit:
                d["unit_id"], d["unit"] = unit.id, unit.symbol
            else:
                issues.append(_newvar_issue(d["source_row"], d["name"], d["unit"], f"Unit '{d['unit']}' is not in the catalogue; the reviewer will choose one.", "warning", "NEW_VARIABLE_UNIT_UNKNOWN", d.get("source_sheet")))

        signature = _name_signature(d["name"])
        best, best_score = None, 0.0
        if signature:
            for v in catalogue:
                if d.get("matrix") and v.matrix and v.matrix != d["matrix"]:
                    continue
                score = SequenceMatcher(None, signature, _name_signature(v.name)).ratio()
                if score > best_score:
                    best, best_score = v, score
        if best is not None and best_score >= SIMILAR_NAME_THRESHOLD:
            d["similar_to"] = {"id": best.id, "name": best.name, "matrix": best.matrix}
            issues.append(_newvar_issue(d["source_row"], d["name"], variable_display_label(best.name, best.matrix), f"'{d['name']}' looks like an existing variable; the reviewer will decide whether to merge them.", "warning", "NEW_VARIABLE_SIMILAR", d.get("source_sheet")))
        staged.append(d)

    if links:
        rename = {f"newvar:{code}": f"var:{vid}" for code, vid in links.items()}
        parsed.column_ids = [rename.get(c, c) for c in parsed.column_ids]
        for row in parsed.rows:
            for old, new in rename.items():
                if old in row:
                    row[new] = row.pop(old)
    return staged, issues


class ImportConflict(Exception):
    """Raised when an import would overwrite data that already exists."""

    def __init__(self, issues: List[dict]) -> None:
        self.issues = issues
        super().__init__(f"{len(issues)} row(s) already exist in the database")


def find_existing_conflicts(db, rows: List[Dict[str, Any]], specs: List[ColumnSpec]) -> List[dict]:
    """Rows whose measurement/attribute already exists in the DB.

    Uploads are insert-only: re-sending a value for an existing (sample, variable)
    pair is a conflict to report, never a silent update. Adding a *new* variable to
    an existing sample stays valid — late lab results are legitimately additive.

    The lookup keys mirror ``_get_or_create_*``/``_upsert_*`` exactly, so what is
    flagged here is precisely what the import would have overwritten.
    """
    # A not-yet-created variable (ref_id None) cannot have a stored value.
    value_specs = [s for s in specs if s.kind in ("var", "attr") and s.ref_id is not None]
    if not rows or not value_specs:
        return []

    # Resolve the natural-key chain in bulk, level by level, so a large file costs
    # a handful of queries instead of one per row.
    codes = {normalize_code(r.get("project_code")) for r in rows}
    codes.discard(None)
    projects = {p.code: p for p in db.query(Project).filter(Project.code.in_(codes)).all()} if codes else {}
    if not projects:
        return []

    project_ids = [p.id for p in projects.values()]
    sites = {
        (s.project_id, s.site_code): s
        for s in db.query(Site).filter(Site.project_id.in_(project_ids)).all()
    }
    if not sites:
        return []

    events = {
        (e.project_id, e.site_id, e.event_date, e.activity): e
        for e in db.query(SamplingEvent).filter(SamplingEvent.project_id.in_(project_ids)).all()
    }
    if not events:
        return []

    event_ids = [e.id for e in events.values()]
    samples = {
        (s.event_id, s.sample_code): s
        for s in db.query(Sample).filter(Sample.event_id.in_(event_ids)).all()
    }
    if not samples:
        return []

    sample_ids = [s.id for s in samples.values()]
    existing_measurements = {
        (m.sample_id, m.variable_id)
        for m in db.query(Measurement.sample_id, Measurement.variable_id)
        .filter(Measurement.sample_id.in_(sample_ids), Measurement.lab_batch_id.is_(None))
        .all()
    }
    existing_attributes = {
        (v.sample_id, v.sample_attribute_id)
        for v in db.query(SampleAttributeValue.sample_id, SampleAttributeValue.sample_attribute_id)
        .filter(SampleAttributeValue.sample_id.in_(sample_ids), SampleAttributeValue.effective_date.is_(None))
        .all()
    }

    issues: List[dict] = []
    for row in rows:
        project = projects.get(normalize_code(row.get("project_code")))
        if project is None:
            continue
        site_code_raw = _clean_str(row.get("site_code"))
        site = sites.get((project.id, normalize_code(site_code_raw) or site_code_raw))
        if site is None:
            continue
        event_date, _ = parse_date(row.get("event_date"))
        event = events.get((project.id, site.id, event_date, _clean_str(row.get("event_activity"))))
        if event is None:
            continue
        sample = samples.get((event.id, _clean_str(row.get("sample_code"))))
        if sample is None:
            continue

        for spec in value_specs:
            if _clean_str(row.get(spec.id)) is None:
                continue
            taken = (
                (sample.id, spec.ref_id) in existing_measurements
                if spec.kind == "var"
                else (sample.id, spec.ref_id) in existing_attributes
            )
            if taken:
                issues.append(
                    _value_issue(
                        row["_row"],
                        spec.id,
                        row.get(spec.id),
                        f"Ya existe un valor para la muestra '{row.get('sample_code')}' en esta columna. "
                        f"Las cargas solo agregan datos nuevos; no sobrescriben.",
                        "error",
                        "ALREADY_EXISTS",
                    )
                )
    return issues


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
                issues.append(_value_issue(row_num, cid, row.get(cid), f"Column '{cid}' is required and this row leaves it empty.", "error", "REQUIRED"))
                rows_with_error.add(row_num)

        # lat / lon
        lat = lon = None
        if "latitude" in present_ids:
            lat, _, ok = parse_numeric(row.get("latitude"))
            if not ok:
                issues.append(_value_issue(row_num, "latitude", row.get("latitude"), "Latitude is not a number. Use decimal degrees with a dot (e.g. 4.6097), not degrees/minutes.", "error", "NOT_NUMERIC"))
                rows_with_error.add(row_num)
            elif lat is not None and not (Decimal("-90") <= lat <= Decimal("90")):
                issues.append(_value_issue(row_num, "latitude", row.get("latitude"), "Latitude out of range (-90 to 90)", "error", "OUT_OF_RANGE"))
                rows_with_error.add(row_num)
        if "longitude" in present_ids:
            lon, _, ok = parse_numeric(row.get("longitude"))
            if not ok:
                issues.append(_value_issue(row_num, "longitude", row.get("longitude"), "Longitude is not a number. Use decimal degrees with a dot (e.g. -74.0817), not degrees/minutes.", "error", "NOT_NUMERIC"))
                rows_with_error.add(row_num)
            elif lon is not None and not (Decimal("-180") <= lon <= Decimal("180")):
                issues.append(_value_issue(row_num, "longitude", row.get("longitude"), "Longitude out of range (-180 to 180)", "error", "OUT_OF_RANGE"))
                rows_with_error.add(row_num)
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 90 and abs(lat) > 50 and abs(lon) < 20:
            issues.append(_value_issue(row_num, "latitude", row.get("latitude"), "Latitude/longitude possibly swapped?", "warning", "POSSIBLE_SWAP"))

        # event_date
        event_date = None
        if "event_date" in present_ids:
            event_date, ok = parse_date(row.get("event_date"))
            if not ok:
                issues.append(_value_issue(row_num, "event_date", row.get("event_date"), DATE_HELP, "error", "INVALID_DATE"))
                rows_with_error.add(row_num)

        # altitude
        if "altitude_m" in present_ids and _clean_str(row.get("altitude_m")) is not None:
            _, _, ok = parse_numeric(row.get("altitude_m"))
            if not ok:
                issues.append(_value_issue(row_num, "altitude_m", row.get("altitude_m"), NUMBER_HELP, "error", "NOT_NUMERIC"))
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
                    issues.append(_value_issue(row_num, "sample_code", sample_code, "Duplicate sample in the file (same event and sample_code)", "error", "DUPLICATE"))
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
            issues.append(_value_issue(row_num, spec.id, raw_value, NUMBER_HELP, "error", "NOT_NUMERIC"))
            rows_with_error.add(row_num)
            return
        # Light range check for pH-like variables.
        if value is not None and spec.code and spec.code.upper().startswith("PH"):
            if not (Decimal("0") <= value <= Decimal("14")):
                issues.append(_value_issue(row_num, spec.id, raw_value, "pH out of range (0-14)", "warning", "OUT_OF_RANGE"))
    elif spec.value_type == "date":
        _, ok = parse_date(raw_value)
        if not ok:
            issues.append(_value_issue(row_num, spec.id, raw_value, DATE_HELP, "error", "INVALID_DATE"))
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


def build_template(
    specs: List[ColumnSpec],
    fmt: str,
    include_examples: bool,
    lang: str = LANG_DEFAULT,
    units: Optional[List[str]] = None,
    domains: Optional[List[str]] = None,
) -> Tuple[bytes, str, str]:
    """Return (content, media_type, filename). ``units`` and ``domains`` feed the
    dropdowns of the xlsx "New variables" sheet (a CSV has no room for that sheet)."""
    if fmt == "csv":
        return _build_template_csv(specs, include_examples, lang)
    return _build_template_xlsx(specs, include_examples, lang, units or [], domains or [])


def _add_new_variables_sheet(wb: Workbook, lang: str, units: List[str], domains: List[str]) -> None:
    """Sheet where the team describes variables the catalog does not have yet, plus
    the hidden sheet that feeds its dropdowns. Dropdowns are hints, not locks:
    anything typed is still validated server-side."""
    lang = lang if lang in NEWVAR_HEADERS else LANG_DEFAULT
    value_type_labels = [VALUE_TYPE_LABELS[lang][vt] for vt in ("numeric", "text", "date", "boolean")]
    matrix_labels = [MATRIX_CHOICE_LABELS[lang][m] for m in MATRIX_ORDER]

    lists = wb.create_sheet(LISTS_SHEET)
    lists.append(["value_type", "matrix", "unit", "domain"])
    for i in range(max(len(value_type_labels), len(matrix_labels), len(units), len(domains))):
        lists.append([
            value_type_labels[i] if i < len(value_type_labels) else None,
            matrix_labels[i] if i < len(matrix_labels) else None,
            units[i] if i < len(units) else None,
            domains[i] if i < len(domains) else None,
        ])
    lists.sheet_state = "hidden"

    ws = wb.create_sheet(NEW_VARIABLES_SHEET[lang])
    instructions = NEWVAR_INSTRUCTIONS[lang]
    for line_no, text in enumerate(instructions, start=1):
        cell = ws.cell(row=line_no, column=1, value=text)
        cell.font = Font(bold=(line_no == 1), size=12 if line_no == 1 else 11)
    header_row = len(instructions) + 2
    for col, label in enumerate(NEWVAR_HEADERS[lang], start=1):
        cell = ws.cell(row=header_row, column=col, value=label)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E8F0E4")
        cell.alignment = Alignment(vertical="center")
    for i, w in enumerate([34, 16, 26, 14, 18, 60], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    first, last = header_row + 1, header_row + NEWVAR_MAX_ROWS
    dropdowns = [
        ("B", f"{LISTS_SHEET}!$A$2:$A${1 + len(value_type_labels)}"),
        ("C", f"{LISTS_SHEET}!$B$2:$B${1 + len(matrix_labels)}"),
    ]
    if units:
        dropdowns.append(("D", f"{LISTS_SHEET}!$C$2:$C${1 + len(units)}"))
    if domains:
        dropdowns.append(("E", f"{LISTS_SHEET}!$D$2:$D${1 + len(domains)}"))
    for column, formula in dropdowns:
        validation = DataValidation(type="list", formula1=formula, allow_blank=True, showErrorMessage=False)
        ws.add_data_validation(validation)
        validation.add(f"{column}{first}:{column}{last}")


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


def _build_template_xlsx(
    specs: List[ColumnSpec],
    include_examples: bool,
    lang: str = LANG_DEFAULT,
    units: Optional[List[str]] = None,
    domains: Optional[List[str]] = None,
) -> Tuple[bytes, str, str]:
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

    # Human-readable glossary sheet (excluded from parsing by name), so whoever
    # fills the template has the meaning, type and destination of every column.
    value_type_labels = VALUE_TYPE_LABELS.get(lang, VALUE_TYPE_LABELS[LANG_DEFAULT])
    yes, no = YES_NO.get(lang, YES_NO[LANG_DEFAULT])
    glos = wb.create_sheet(META_SHEET)
    glos.append(GLOSSARY_HEADERS.get(lang, GLOSSARY_HEADERS[LANG_DEFAULT]))
    for cell in glos[1]:
        cell.font = Font(bold=True)
    for s in specs:
        glos.append([
            s.label,
            s.id,
            s.db_target or "",
            value_type_labels.get(s.value_type, s.value_type),
            yes if s.required else no,
            s.unit or "",
            s.description or "",
            _example_for(s),
        ])
    for i, w in enumerate([34, 24, 26, 10, 12, 10, 60, 16], start=1):
        glos.column_dimensions[get_column_letter(i)].width = w
    glos.freeze_panes = "A2"

    _add_new_variables_sheet(wb, lang, units or [], domains or [])

    buffer = io.BytesIO()
    wb.save(buffer)
    filename = f"{TEMPLATE_FILENAME.get(lang, TEMPLATE_FILENAME[LANG_DEFAULT])}.xlsx"
    return (
        buffer.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename,
    )


def _build_template_csv(specs: List[ColumnSpec], include_examples: bool, lang: str = LANG_DEFAULT) -> Tuple[bytes, str, str]:
    import csv

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    # CSV cannot hide rows, so the header row IS the technical id (self-describing
    # for re-upload). A second commented line documents the human label.
    writer.writerow([s.id for s in specs])
    writer.writerow([f"# {s.label}" for s in specs])
    if include_examples:
        writer.writerow([_example_for(s) for s in specs])
    filename = f"{TEMPLATE_FILENAME.get(lang, TEMPLATE_FILENAME[LANG_DEFAULT])}.csv"
    return buffer.getvalue().encode("utf-8-sig"), "text/csv", filename


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
    # The uploader's definitions for ``newvar:`` columns (see process_new_variables).
    new_variables: List[dict] = field(default_factory=list)


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


class VariableResolutionError(ValueError):
    """A reviewer's decision about a new variable cannot be applied (bad merge
    target, type mismatch, invalid matrix/unit). The message is safe to show."""


def _materialize_new_variables(
    db, specs: List[ColumnSpec], stored: "StoredUpload", resolutions: Dict[str, dict]
) -> int:
    """Create (or merge into existing) the variables behind ``newvar:`` columns.

    A reviewer may, per variable, ``merge`` it into an existing one or ``create`` it
    with edits (name / matrix / unit / description). Without a decision it is created
    exactly as the uploader described it. Returns how many variables were created.
    Sets ``spec.ref_id`` so the normal measurement path takes over.
    """
    created = 0
    for spec in specs:
        if spec.kind != "var" or spec.ref_id is not None or not spec.new_def:
            continue
        d = spec.new_def
        decision = resolutions.get(d["code"]) or {}

        if decision.get("action") == "merge":
            target = db.query(Variable).filter(Variable.id == decision.get("variable_id")).first()
            if target is None:
                raise VariableResolutionError(f"The variable chosen to merge '{d['name']}' into does not exist.")
            if target.value_type != d["value_type"]:
                raise VariableResolutionError(
                    f"'{d['name']}' holds {d['value_type']} values but '{target.name}' is {target.value_type}; they cannot be merged."
                )
            spec.ref_id, spec.code, spec.value_type = target.id, target.variable_code, target.value_type
            continue

        name = (decision.get("name") or d["name"]).strip()
        matrix = decision["matrix"] if "matrix" in decision else d.get("matrix")
        matrix = matrix or None
        if matrix is not None and matrix not in VALID_MATRICES:
            raise VariableResolutionError(f"Unknown matrix '{matrix}' for '{name}'.")
        unit_id = decision["unit_id"] if "unit_id" in decision else d.get("unit_id")
        if unit_id is not None and db.query(Unit.id).filter(Unit.id == unit_id).first() is None:
            raise VariableResolutionError(f"The unit chosen for '{name}' does not exist.")
        description = decision["description"] if "description" in decision else d.get("description")
        domain = normalize_code(decision.get("domain") if "domain" in decision else d.get("domain")) or DEFAULT_VARIABLE_DOMAIN

        edited = name != d["name"] or matrix != d.get("matrix")
        code = (derive_variable_code(name, matrix) if edited else d["code"]) or d["code"]

        existing = db.query(Variable).filter(Variable.variable_code == code).first()
        if existing is not None:
            # Same code already taken (another submission got there first, or the
            # reviewer's edit collides): reuse it only if it is the same kind of value.
            if existing.value_type != d["value_type"]:
                raise VariableResolutionError(
                    f"Code '{code}' already belongs to '{existing.name}', which holds {existing.value_type} values."
                )
            spec.ref_id, spec.code = existing.id, existing.variable_code
            continue

        variable = Variable(
            variable_code=code,
            name=name,
            domain=domain,
            matrix=matrix,
            value_type=d["value_type"],
            default_unit_id=unit_id,
            description=description,
            notes={"source": "upload", "source_file": stored.filename, "upload_id": stored.upload_id},
        )
        db.add(variable)
        db.flush()
        spec.ref_id, spec.code = variable.id, variable.variable_code
        created += 1
    return created


def import_upload(db, stored: StoredUpload, variable_resolutions: Optional[Dict[str, dict]] = None) -> dict:
    """Persist a previously-validated upload. Caller wraps commit/rollback.

    Insert-only: raises ``ImportConflict`` if any row would overwrite existing
    data. This is re-checked here (not only at validation time) because an upload
    waits for review, and another submission may have been approved meanwhile.

    ``variable_resolutions`` maps a new variable's code to the reviewer's decision
    (see ``_materialize_new_variables``).
    """
    specs, _ = resolve_columns(db, stored.column_ids, new_variables=stored.new_variables)
    spec_by_id = {s.id: s for s in specs}

    # New variables first: once they exist (or are merged), their columns are
    # ordinary variable columns — including for the conflict check below.
    new_variables_created = _materialize_new_variables(db, specs, stored, variable_resolutions or {})

    conflicts = find_existing_conflicts(db, stored.rows, specs)
    if conflicts:
        raise ImportConflict(conflicts)

    inserted = {"projects": 0, "sites": 0, "sampling_events": 0, "samples": 0, "measurements": 0, "new_variables": new_variables_created}
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
    projects_affected = [
        {"id": p.id, "code": p.code, "name": p.name} for p in projects_cache.values()
    ]
    return {"inserted": inserted, "skipped_rows": skipped_rows, "projects_affected": projects_affected}


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
    # Insert-only: never touch a value that is already stored. Conflicts are
    # caught up-front by find_existing_conflicts; this is the last line of defence.
    if measurement:
        return False
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
    # Insert-only, same rule as measurements.
    if value:
        return
    value = SampleAttributeValue(sample_id=sample.id, sample_attribute_id=spec.ref_id)
    db.add(value)
    value.value_numeric = None
    value.value_text = None
    value.value_boolean = None
    value.value_date = None
    _assign_typed_value(value, spec.value_type, raw_value)
    value.notes = _trace_notes(source_file, row_num, raw_value)
    db.flush()
