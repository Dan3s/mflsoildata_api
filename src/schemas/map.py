from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class SiteMapItem(BaseModel):
    id: int
    site_code: str
    project_id: int
    project_name: str
    country: Optional[str] = None
    admin_1: Optional[str] = None
    admin_2: Optional[str] = None
    altitude_m: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    measurement_count: int
    last_event_date: Optional[date] = None
    last_updated: Optional[datetime] = None
    is_selected: bool = False
    is_active: bool = True
    # Per-site mean of the selected "color by variable" (null when not requested
    # or the site has no measurements for that variable).
    color_value: Optional[float] = None

    model_config = ConfigDict(from_attributes=True)


class SiteAttributeItem(BaseModel):
    attribute_code: str
    attribute_name: str
    value: Any = None
    value_type: str
    unit: Optional[str] = None
    effective_date: Optional[date] = None
    source_name: Optional[str] = None


class SiteMeasurementSummary(BaseModel):
    variable_id: int
    variable_name: str
    variable_code: str
    value: Any = None
    unit: Optional[str] = None
    event_date: Optional[date] = None
    updated_at: Optional[datetime] = None


class RecentMeasurementItem(BaseModel):
    id: int
    measurement_code: Optional[str] = None
    variable_id: int
    variable_name: str
    value: Any = None
    unit: Optional[str] = None
    event_date: Optional[datetime] = None


class SiteMapDetail(BaseModel):
    id: int
    site_code: str
    project_id: int
    project_name: str
    country: Optional[str] = None
    admin_1: Optional[str] = None
    admin_2: Optional[str] = None
    admin_3: Optional[str] = None
    altitude_m: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    measurement_count: int
    last_event_date: Optional[date] = None
    last_updated: Optional[datetime] = None
    attributes: list[SiteAttributeItem] = []
    recent_variables: list[SiteMeasurementSummary] = []
    recent_measurements: list[RecentMeasurementItem] = []


class MapSummary(BaseModel):
    total_sites: int
    active_sites: int
    inactive_sites: int
    selected_sites: int = 0
    date_from: Optional[date] = None
    date_to: Optional[date] = None


class MapExportResponse(BaseModel):
    filename: str
    rows: int
