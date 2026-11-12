"""Pydantic schemas for the bulk upload (carga de datos) flow."""

from typing import Any, List, Optional

from pydantic import BaseModel


class TemplateColumn(BaseModel):
    id: str
    label: str
    required: bool = False
    value_type: str
    unit: Optional[str] = None
    db_target: Optional[str] = None
    example: Optional[str] = None
    # Present for variable / attribute columns
    variable_id: Optional[int] = None
    variable_code: Optional[str] = None
    matrix: Optional[str] = None
    attribute_code: Optional[str] = None


class ColumnMappingEntry(BaseModel):
    """One row of an explicit, user-built column mapping (see the column-mapping
    upload UI): "the column at this position in my file is this target field"."""

    source_index: int  # 0-based column position in the uploaded file's header row
    source_header: Optional[str] = None  # original header text; traceability only, not authoritative
    target_id: Optional[str] = None  # our column id ("project_code", "attr:...", "var:..."); None/"" = ignore


class TemplateColumnGroup(BaseModel):
    key: str
    label: str
    description: Optional[str] = None
    columns: List[TemplateColumn]


class TemplateColumnsResponse(BaseModel):
    groups: List[TemplateColumnGroup]


class ValidationIssue(BaseModel):
    row: int
    column: Optional[str] = None
    value: Optional[str] = None
    message: str
    severity: str  # error | warning
    code: str
    # Set when ``row`` belongs to another sheet than the data one (the "New variables"
    # sheet). ``row`` 0 means the problem concerns the file or a column, not a row.
    sheet: Optional[str] = None


class UploadSummary(BaseModel):
    projects: int = 0
    sites: int = 0
    sampling_events: int = 0
    samples: int = 0
    measurements: int = 0
    new_variables: int = 0


class ValidateResponse(BaseModel):
    upload_id: str
    filename: str
    total_rows: int
    valid_rows: int
    error_count: int
    warning_count: int
    summary: UploadSummary
    issues: List[ValidationIssue]
    # Derived from the columns actually present in the file, so the metadata form
    # can prefill "expected matrices/variables" instead of asking the user to
    # retype what the spreadsheet already states.
    detected_matrices: List[str] = []
    detected_variables: List[str] = []


class ImportRequest(BaseModel):
    upload_id: str
    ignore_warnings: bool = True
    # FAIR metadata for the affected project, filled in the upload wizard. Staged
    # with the submission and applied on approval (the project may not exist yet).
    metadata: Optional[dict] = None
    # Set when the upload adds data to a dataset that is already in the platform:
    # every row must belong to it, and the submission is tied to it from the start.
    project_code: Optional[str] = None


class ImportResponse(BaseModel):
    status: str
    inserted: UploadSummary
    skipped_rows: int = 0
    message: str
