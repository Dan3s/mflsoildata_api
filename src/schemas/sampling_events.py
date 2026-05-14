from typing import Optional
from pydantic import BaseModel
from datetime import datetime


class SamplingEventBase(BaseModel):
    event_type: Optional[str] = None
    activity: Optional[str] = None
    organization: Optional[str] = None
    responsible: Optional[str] = None


class SamplingEventCreate(SamplingEventBase):
    project_id: int
    site_id: int
    event_date: datetime


class SamplingEventRead(SamplingEventBase):
    id: int
    project_id: int
    site_id: int
    event_date: datetime
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
