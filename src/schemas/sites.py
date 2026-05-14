from typing import Optional
from pydantic import BaseModel
from datetime import datetime


class SiteBase(BaseModel):
    site_code: str
    country: Optional[str] = None
    admin_1: Optional[str] = None
    admin_2: Optional[str] = None
    admin_3: Optional[str] = None
    altitude_m: Optional[float] = None


class SiteCreate(SiteBase):
    project_id: int
    latitude: float
    longitude: float


class SiteRead(SiteBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SiteUpdate(BaseModel):
    site_code: Optional[str] = None
    country: Optional[str] = None
    admin_1: Optional[str] = None
    admin_2: Optional[str] = None
    admin_3: Optional[str] = None
    altitude_m: Optional[float] = None
