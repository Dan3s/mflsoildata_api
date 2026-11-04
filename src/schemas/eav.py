"""Schemas for dynamic attributes (EAV) and soil profiles.

Reused across site / sample / soil-profile attributes, which share the same
shape in the ORM (code, name, description, value_type, default_unit_id,
category) and the same value columns (value_numeric/text/boolean/date +
effective_date + unit_id + source_name/ref + confidence + notes).
"""

from datetime import date as date_type
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

VALUE_TYPES = {"numeric", "text", "boolean", "date"}


# ── Attribute definitions ────────────────────────────────────────────────────

class AttributeDefinitionCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    value_type: str  # numeric | text | boolean | date
    default_unit_id: Optional[int] = None
    category: Optional[str] = None


class AttributeDefinitionRead(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    value_type: str
    default_unit_id: Optional[int] = None
    category: Optional[str] = None


# ── Attribute values ─────────────────────────────────────────────────────────

class AttributeValueBase(BaseModel):
    value_numeric: Optional[float] = None
    value_text: Optional[str] = None
    value_boolean: Optional[bool] = None
    value_date: Optional[date_type] = None
    unit_id: Optional[int] = None
    effective_date: Optional[date_type] = None
    source_name: Optional[str] = None
    source_ref: Optional[str] = None
    confidence: Optional[float] = None
    notes: Optional[Any] = None


class SiteAttributeValueCreate(AttributeValueBase):
    site_id: int
    site_attribute_id: int


class SampleAttributeValueCreate(AttributeValueBase):
    sample_id: int
    sample_attribute_id: int


class SoilProfileAttributeValueCreate(AttributeValueBase):
    soil_profile_id: int
    soil_profile_attribute_id: int


class AttributeValueRead(BaseModel):
    id: int
    value_numeric: Optional[float] = None
    value_text: Optional[str] = None
    value_boolean: Optional[bool] = None
    value_date: Optional[date_type] = None
    unit_id: Optional[int] = None
    effective_date: Optional[date_type] = None
    source_name: Optional[str] = None
    source_ref: Optional[str] = None
    confidence: Optional[float] = None

    model_config = ConfigDict(from_attributes=True)


# ── Soil profiles ────────────────────────────────────────────────────────────

class SoilProfileCreate(BaseModel):
    project_id: int
    profile_code: str
    site_id: Optional[int] = None
    event_id: Optional[int] = None
    notes: Optional[Any] = None


class SoilProfileRead(BaseModel):
    id: int
    project_id: int
    profile_code: str
    site_id: Optional[int] = None
    event_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)
