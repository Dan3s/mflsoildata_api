from typing import Optional
from pydantic import BaseModel
from datetime import datetime


class VariableBase(BaseModel):
    variable_code: str
    name: str
    domain: str
    value_type: str  # 'numeric', 'text', 'boolean', 'date'
    matrix: Optional[str] = None  # SOIL, LEAF, GRAIN


class VariableCreate(VariableBase):
    default_unit_id: Optional[int] = None
    description: Optional[str] = None


class VariableRead(VariableBase):
    id: int
    default_unit_id: Optional[int] = None
    description: Optional[str] = None

    class Config:
        from_attributes = True


class VariableUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    domain: Optional[str] = None
