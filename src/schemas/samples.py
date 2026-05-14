from typing import Optional
from pydantic import BaseModel
from datetime import datetime


class SampleBase(BaseModel):
    sample_code: str
    sample_type_id: int


class SampleCreate(SampleBase):
    event_id: Optional[int] = None
    site_id: Optional[int] = None
    parent_sample_id: Optional[int] = None


class SampleRead(SampleBase):
    id: int
    event_id: Optional[int] = None
    site_id: Optional[int] = None
    parent_sample_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SampleUpdate(BaseModel):
    sample_code: Optional[str] = None
    parent_sample_id: Optional[int] = None
