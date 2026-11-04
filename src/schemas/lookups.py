# Aliased: the DashboardRecord field is named `date`, which would shadow the
# `date` type. In an annotated assignment (`date: Optional[date] = None`) Python
# binds the value before evaluating the annotation, so `date` would resolve to
# None and Pydantic would reject real dates. The alias avoids that collision.
from datetime import date as date_type
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class LookupItem(BaseModel):
    id: int
    label: str


class MatrixItem(BaseModel):
    code: str
    label: str


class VariableLookupItem(BaseModel):
    id: int
    label: str
    variable_code: str
    domain: str
    matrix: Optional[str] = None
    value_type: str

    model_config = ConfigDict(from_attributes=True)


class DashboardSummary(BaseModel):
    projects: int
    sites: int
    samples: int
    variables: int
    measurements: int


class PublicSummary(BaseModel):
    """Counts for the anonymous landing-page teaser — PUBLIC-access data only."""

    datasets: int
    sites: int
    samples: int
    countries: int


class DashboardRecord(BaseModel):
    measurement_id: int
    measurement_code: Optional[str] = None
    project_id: int
    project_code: str
    project_name: str
    site_id: Optional[int] = None
    site_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    sample_id: int
    sample_code: str
    sample_type_id: int
    sample_type: str
    matrix: Optional[str] = None
    variable_id: int
    variable_code: str
    variable_name: str
    value: Any = None
    unit: Optional[str] = None
    date: Optional[date_type] = None
    depth_from: Optional[float] = None
    depth_to: Optional[float] = None
    depth_cm: Optional[float] = None
    laboratory: Optional[str] = None
    status: Optional[str] = None


class DashboardRecordsResponse(BaseModel):
    items: list[DashboardRecord]
    total: int
    skip: int
    limit: int


class AgrovocTerm(BaseModel):
    """A concept label from FAO's AGROVOC thesaurus."""

    label: str
    uri: Optional[str] = None
