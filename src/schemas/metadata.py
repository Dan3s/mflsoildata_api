"""Pydantic schemas for project metadata (FAIR).

See docs/PROPUESTA_METADATOS.md. Enumerated values are validated here (light
validation) since the DB stores them as text. Fields will evolve as confirmed.
"""

from datetime import date, datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, field_validator

ACCESS_LEVELS = {"PUBLIC", "RESTRICTED", "PRIVATE", "EMBARGOED", "LICENSED", "CONFIDENTIAL"}
QC_STATUSES = {"NOT_REVIEWED", "IN_REVIEW", "PASSED", "FAILED"}
# Geometry a dataset describes. The platform stores point sites today; the other
# values let a dataset declare coverage that is not a set of points.
SPATIAL_DATA_TYPES = {"POINT", "LINE", "POLYGON", "RASTER"}

# Dataverse's own "Subject" controlled vocabulary (citation.tsv), offered as
# suggestions in the UI. Stored as free text (not validated): a dataset
# imported from Dataverse may carry a value we don't enumerate here.
DATAVERSE_SUBJECTS = [
    "Agricultural Sciences",
    "Arts and Humanities",
    "Astronomy and Astrophysics",
    "Business and Management",
    "Chemistry",
    "Computer and Information Science",
    "Earth and Environmental Sciences",
    "Engineering",
    "Law",
    "Mathematical Sciences",
    "Medicine, Health and Life Sciences",
    "Physics",
    "Social Sciences",
    "Other",
]


class KeywordTerm(BaseModel):
    """A dataset keyword, ideally anchored to an AGROVOC concept.

    ``label`` is what a person reads; ``uri`` is the concept's permanent
    identifier, which is what makes the keyword machine-resolvable (FAIR I2/I3):
    "soil" (en) and "Suelo" (es) are the same concept c_7156, and only the URI
    says so.
    """

    label: str
    uri: Optional[str] = None


def normalize_keywords(value):
    """Accept both shapes and always store [{label, uri}].

    Datasets created before AGROVOC support hold plain strings, and the catalogue
    edit form can still submit them, so both have to keep working. A plain string
    simply becomes a term with no concept URI.
    """
    if value is None:
        return None
    if isinstance(value, (str, dict)):
        value = [value]
    terms = []
    for item in value:
        if isinstance(item, str):
            label, uri = item.strip(), None
        elif isinstance(item, dict):
            label, uri = str(item.get("label") or "").strip(), item.get("uri") or None
        else:
            continue
        if label and not any(t["label"].lower() == label.lower() for t in terms):
            terms.append({"label": label, "uri": uri})
    return terms or None


class ProjectMetadataBase(BaseModel):
    # Findable
    title: Optional[str] = None
    abstract: Optional[str] = None
    keywords: Optional[Any] = None
    persistent_id: Optional[str] = None
    related_publications: Optional[Any] = None
    version: Optional[str] = None
    # Coverage
    temporal_coverage_start: Optional[date] = None
    temporal_coverage_end: Optional[date] = None
    spatial_coverage: Optional[str] = None
    spatial_data_type: Optional[str] = None
    expected_matrices: Optional[Any] = None
    expected_variables: Optional[str] = None
    sampling_design: Optional[str] = None
    # Accessible / privacy
    access_level: Optional[str] = None
    authorized_users: Optional[Any] = None
    embargo_until: Optional[date] = None
    license: Optional[str] = None
    access_conditions: Optional[str] = None
    # Reusable / provenance
    data_source: Optional[str] = None
    provenance: Optional[str] = None
    methodology: Optional[str] = None
    citation: Optional[str] = None
    usage_notes: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    # Backup contact, in case the primary one doesn't answer.
    contact_name_2: Optional[str] = None
    contact_email_2: Optional[str] = None
    # Authors/creators (for citation), distinct from the contact above.
    authors: Optional[Any] = None
    funding_information: Optional[str] = None
    language: Optional[str] = None
    related_datasets: Optional[Any] = None
    subject: Optional[str] = None
    geographic_bounding_box: Optional[Any] = None
    # Quality control
    quality_controlled: Optional[bool] = None
    qc_status: Optional[str] = None
    qc_date: Optional[date] = None
    qc_by: Optional[str] = None
    qc_notes: Optional[str] = None
    # System
    data_last_updated: Optional[date] = None
    notes: Optional[Any] = None

    @field_validator("keywords", mode="before")
    @classmethod
    def _normalize_keywords(cls, v):
        return normalize_keywords(v)

    @field_validator("access_level")
    @classmethod
    def _validate_access_level(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ACCESS_LEVELS:
            raise ValueError(f"access_level must be one of {sorted(ACCESS_LEVELS)}")
        return v

    @field_validator("spatial_data_type")
    @classmethod
    def _validate_spatial_data_type(cls, v):
        if v is not None and v not in SPATIAL_DATA_TYPES:
            raise ValueError(f"spatial_data_type must be one of {sorted(SPATIAL_DATA_TYPES)}")
        return v

    @field_validator("qc_status")
    @classmethod
    def _validate_qc_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in QC_STATUSES:
            raise ValueError(f"qc_status must be one of {sorted(QC_STATUSES)}")
        return v


class ProjectMetadataUpsert(ProjectMetadataBase):
    """Payload for creating/updating metadata (partial: only sent fields change)."""


# Quality-control fields are filled by the reviewing team, never by whoever uploads
# the data (agreed with the data management team). Enforced in the endpoint, not
# just hidden in the UI.
QC_FIELDS = ("quality_controlled", "qc_status", "qc_date", "qc_by", "qc_notes")


class ProjectMetadataRead(ProjectMetadataBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    # Always present (DB defaults), so narrow the optional base types.
    access_level: str
    quality_controlled: bool
    qc_status: str
    created_at: datetime
    updated_at: datetime


class DatasetCatalogueItem(BaseModel):
    """Compact per-project row for the dataset catalogue (project + metadata)."""

    project_id: int
    code: str
    name: str
    # Countries come from the project's sites (a project can span several).
    countries: List[str] = []
    title: Optional[str] = None
    abstract: Optional[str] = None
    access_level: Optional[str] = None
    quality_controlled: Optional[bool] = None
    qc_status: Optional[str] = None
    keywords: Optional[Any] = None
    expected_matrices: Optional[Any] = None
    data_source: Optional[str] = None
    version: Optional[str] = None
    temporal_coverage_start: Optional[date] = None
    temporal_coverage_end: Optional[date] = None
    has_metadata: bool = False
    updated_at: Optional[datetime] = None
