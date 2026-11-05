"""Pydantic schemas for the data download (descarga de datos) flow."""

from datetime import date, datetime
from typing import Dict, List, Optional

from pydantic import BaseModel


class DownloadEstimate(BaseModel):
    record_count: int
    sample_count: int
    site_count: int


class DownloadFacets(BaseModel):
    """What is actually available under the current choices, so each selector can offer
    only options that have data (and the date filter can show its real bounds)."""

    project_ids: List[int]
    variable_ids: List[int]
    # How many measurements each of those variables has in the chosen scope, so a caller can
    # start from the ones with the most data instead of an arbitrary few.
    variable_counts: Dict[int, int] = {}
    date_min: Optional[date] = None
    date_max: Optional[date] = None


class RecentDownload(BaseModel):
    id: int
    filename: str
    created_at: datetime
    format: str
    record_count: Optional[int] = None
    # Admin audit view: who ran the export and from where.
    username: Optional[str] = None
    user_email: Optional[str] = None
    organization: Optional[str] = None
    country: Optional[str] = None
