"""Pydantic schemas for the data download (descarga de datos) flow."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DownloadEstimate(BaseModel):
    record_count: int
    sample_count: int
    site_count: int


class RecentDownload(BaseModel):
    id: int
    filename: str
    created_at: datetime
    format: str
    record_count: Optional[int] = None
