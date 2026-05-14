from typing import Optional
from pydantic import BaseModel
from datetime import datetime


class MeasurementBase(BaseModel):
    measurement_code: Optional[str] = None
    variable_id: int
    lab_batch_id: Optional[int] = None
    unit_id: Optional[int] = None
    qualifier_result: Optional[str] = None


class MeasurementCreate(MeasurementBase):
    sample_id: int
    value_numeric: Optional[float] = None
    value_text: Optional[str] = None
    value_boolean: Optional[bool] = None
    value_date: Optional[datetime] = None


class MeasurementRead(MeasurementBase):
    id: int
    sample_id: int
    value_numeric: Optional[float] = None
    value_text: Optional[str] = None
    value_boolean: Optional[bool] = None
    value_date: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
