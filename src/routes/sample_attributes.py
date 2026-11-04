"""Sample attribute (EAV) endpoints — RF08 / RF09.

Register dynamic sample attributes and their values. Reads are Viewer+, writes
require Editor/Admin.
"""

from typing import List

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import Sample, SampleAttribute, SampleAttributeValue

from schemas.eav import (
    AttributeDefinitionCreate,
    AttributeDefinitionRead,
    AttributeValueRead,
    SampleAttributeValueCreate,
)
from services import eav
from dependencies.auth import CurrentUser, get_current_user, require_editor, require_database_editor
from dependencies.access import accessible_project_ids

router = APIRouter(tags=["Sample attributes"])


@router.get("/sample-attributes", response_model=List[AttributeDefinitionRead])
async def list_sample_attributes():
    """Attribute definitions that can be recorded about a sample (depth, horizon...)."""
    db = SessionLocal()
    try:
        rows = db.query(SampleAttribute).order_by(SampleAttribute.name.asc()).all()
        return [eav.definition_read(row, "sample_attribute_code") for row in rows]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post(
    "/sample-attributes",
    response_model=AttributeDefinitionRead,
    status_code=201,
    dependencies=[Depends(require_database_editor)],
)
async def create_sample_attribute(payload: AttributeDefinitionCreate = Body(...)):
    """Define a new sample attribute. Extends the catalogue, so database_editor only."""
    db = SessionLocal()
    try:
        obj = eav.create_attribute_definition(db, SampleAttribute, "sample_attribute_code", payload)
        return eav.definition_read(obj, "sample_attribute_code")
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/sample-attribute-values", response_model=List[AttributeValueRead])
async def list_sample_attribute_values(
    sample_id: int = Query(..., description="Sample ID"),
    user: CurrentUser = Depends(get_current_user),
):
    """Attribute values recorded for one sample."""
    db = SessionLocal()
    try:
        sample = db.query(Sample).filter(Sample.id == sample_id).first()
        if not sample:
            raise HTTPException(status_code=404, detail="sample_id no existe.")
        allowed = accessible_project_ids(db, user)
        if allowed is not None:
            candidate_ids = {
                pid
                for pid in (
                    sample.site.project_id if sample.site else None,
                    sample.event.project_id if sample.event else None,
                )
                if pid is not None
            }
            if not candidate_ids & allowed:
                raise HTTPException(status_code=404, detail="sample_id no existe.")
        return (
            db.query(SampleAttributeValue)
            .filter(SampleAttributeValue.sample_id == sample_id)
            .order_by(SampleAttributeValue.effective_date.asc().nullsfirst())
            .all()
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post(
    "/sample-attribute-values",
    response_model=AttributeValueRead,
    status_code=201,
    dependencies=[Depends(require_editor)],
)
async def create_sample_attribute_value(payload: SampleAttributeValueCreate = Body(...)):
    """Record an attribute value for a sample."""
    db = SessionLocal()
    try:
        if db.query(Sample).filter(Sample.id == payload.sample_id).first() is None:
            raise HTTPException(status_code=404, detail="sample_id no existe.")
        if db.query(SampleAttribute).filter(SampleAttribute.id == payload.sample_attribute_id).first() is None:
            raise HTTPException(status_code=404, detail="sample_attribute_id no existe.")
        return eav.create_attribute_value(
            db,
            SampleAttributeValue,
            payload,
            {"sample_id": payload.sample_id, "sample_attribute_id": payload.sample_attribute_id},
        )
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()
