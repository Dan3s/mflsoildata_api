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


class ImportRequest(BaseModel):
    upload_id: str
    ignore_warnings: bool = True


class ImportResponse(BaseModel):
    status: str
    inserted: UploadSummary
    skipped_rows: int = 0
    message: str
