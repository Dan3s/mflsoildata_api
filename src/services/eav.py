"""Shared helpers for EAV attribute definitions and values.

Used by the site / sample / soil-profile attribute routers so the create logic
(code normalization, uniqueness, single-value enforcement) lives in one place.
"""

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from schemas.eav import VALUE_TYPES
from services.etl import normalize_code


def create_attribute_definition(db, model, code_field: str, payload):
    """Create an attribute definition (site/sample/soil-profile). Normalizes the
    code, enforces uniqueness and a valid value_type."""
    code = normalize_code(payload.code) or normalize_code(payload.name)
    if not code:
        raise HTTPException(status_code=400, detail="Debe indicar 'code' o 'name'.")
    if payload.value_type not in VALUE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"value_type inválido. Use uno de: {sorted(VALUE_TYPES)}",
        )
    if db.query(model).filter(getattr(model, code_field) == code).first():
        raise HTTPException(status_code=409, detail=f"Ya existe un atributo con código '{code}'.")

    obj = model(
        **{code_field: code},
        name=payload.name,
        description=payload.description,
        value_type=payload.value_type,
        default_unit_id=payload.default_unit_id,
        category=payload.category,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def definition_read(obj, code_field: str) -> dict:
    return {
        "id": obj.id,
        "code": getattr(obj, code_field),
        "name": obj.name,
        "description": obj.description,
        "value_type": obj.value_type,
        "default_unit_id": obj.default_unit_id,
        "category": obj.category,
    }


def _value_count(payload) -> int:
    return sum(
        value is not None
        for value in (payload.value_numeric, payload.value_text, payload.value_boolean, payload.value_date)
    )


def create_attribute_value(db, model, payload, keys: dict):
    """Create an attribute value. ``keys`` holds the entity + attribute foreign
    keys (e.g. {"site_id": ..., "site_attribute_id": ...})."""
    if _value_count(payload) != 1:
        raise HTTPException(
            status_code=422,
            detail="Debe enviar exactamente un valor (value_numeric, value_text, value_boolean o value_date).",
        )

    obj = model(
        **keys,
        value_numeric=payload.value_numeric,
        value_text=payload.value_text,
        value_boolean=payload.value_boolean,
        value_date=payload.value_date,
        unit_id=payload.unit_id,
        effective_date=payload.effective_date,
        source_name=payload.source_name,
        source_ref=payload.source_ref,
        confidence=payload.confidence,
        notes=payload.notes,
    )
    db.add(obj)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe un valor para esa combinación (entidad, atributo, fecha efectiva).",
        )
    db.refresh(obj)
    return obj
