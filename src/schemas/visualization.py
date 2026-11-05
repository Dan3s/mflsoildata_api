from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict


class TimeSeriesPoint(BaseModel):
    variable_id: int
    variable_name: str
    values: list[Optional[float]]


class TimeSeriesResponse(BaseModel):
    labels: list[str]
    series: list[TimeSeriesPoint]


class SiteSeriesPoint(BaseModel):
    name: str
    values: list[Optional[float]]


class BySiteResponse(BaseModel):
    categories: list[str]
    series: list[SiteSeriesPoint]


class CorrelationPoint(BaseModel):
    x: float
    y: float


class CorrelationResponse(BaseModel):
    points: list[CorrelationPoint]
    correlation: Optional[float] = None


class ExportResponse(BaseModel):
    chart_type: str
    filename: str


class VariableOption(BaseModel):
    id: int
    label: str
    variable_code: str
    domain: str
    matrix: Optional[str] = None
    value_type: str

    model_config = ConfigDict(from_attributes=True)
