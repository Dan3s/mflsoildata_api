from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict


class SubmissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: Optional[int] = None
    project_code: Optional[str] = None
    project_name: Optional[str] = None
    project_codes: Optional[List[str]] = None
    filename: Optional[str] = None
    total_rows: Optional[int] = None
    # Variables the uploader defined (not in the catalogue until approval); the
    # reviewer can edit or merge each one when approving.
    new_variables: Optional[List[Dict[str, Any]]] = None
    summary: Optional[Any] = None      # dry-run estimate (before approval)
    warning_count: Optional[int] = None
    submitted_by_email: Optional[str] = None
    submitted_by_name: Optional[str] = None
    organization: Optional[str] = None
    country: Optional[str] = None
    status: str
    inserted: Optional[Any] = None
    submitted_at: datetime
    reviewed_at: Optional[datetime] = None
    reviewed_by_email: Optional[str] = None
    reviewed_by_name: Optional[str] = None
    review_notes: Optional[str] = None


class SubmissionRowRead(BaseModel):
    row_number: Optional[int] = None
    project_code: Optional[str] = None
    site_code: Optional[str] = None
    sample_code: Optional[str] = None
    data: Dict[str, Any] = {}


class SubmissionRowsResponse(BaseModel):
    total: int
    offset: int
    limit: int
    rows: List[SubmissionRowRead]


class VariableResolution(BaseModel):
    """The reviewer's decision for one new variable (identified by its ``code``).

    ``merge`` sends the uploader's column into an existing variable; ``create`` adds
    it to the catalogue, optionally with edits. Only the fields actually sent
    override the uploader's values (a ``null`` clears matrix / unit / description).
    """

    code: str
    action: Literal["create", "merge"] = "create"
    variable_id: Optional[int] = None  # merge target
    name: Optional[str] = None
    matrix: Optional[str] = None
    unit_id: Optional[int] = None
    domain: Optional[str] = None
    description: Optional[str] = None


class ReviewRequest(BaseModel):
    notes: Optional[str] = None
    variable_resolutions: Optional[List[VariableResolution]] = None
