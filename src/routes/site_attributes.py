"""Site attribute (EAV) endpoints — RF04 / RF05.

Register dynamic geographic attributes and their values for sites. Reads are
Viewer+, writes require Editor/Admin.
"""

from typing import List

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import Site, SiteAttribute, SiteAttributeValue

from schemas.eav import (
    AttributeDefinitionCreate,
    AttributeDefinitionRead,
    AttributeValueRead,
    SiteAttributeValueCreate,
)
from services import eav
from dependencies.auth import CurrentUser, get_current_user, require_editor, require_database_editor
from dependencies.access import accessible_project_ids

router = APIRouter(tags=["Site attributes"])


@router.get("/site-attributes", response_model=List[AttributeDefinitionRead])
async def list_site_attributes():
    """Attribute definitions that can be recorded about a site."""
    db = SessionLocal()
    try:
        rows = db.query(SiteAttribute).order_by(SiteAttribute.name.asc()).all()
        return [eav.definition_read(row, "site_attribute_code") for row in rows]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post(
    "/site-attributes",
    response_model=AttributeDefinitionRead,
    status_code=201,
    dependencies=[Depends(require_database_editor)],
)
async def create_site_attribute(payload: AttributeDefinitionCreate = Body(...)):
    """Define a new site attribute. Extends the catalogue, so database_editor only."""
    db = SessionLocal()
    try:
        obj = eav.create_attribute_definition(db, SiteAttribute, "site_attribute_code", payload)
        return eav.definition_read(obj, "site_attribute_code")
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/site-attribute-values", response_model=List[AttributeValueRead])
async def list_site_attribute_values(
    site_id: int = Query(..., description="Site ID"),
    user: CurrentUser = Depends(get_current_user),
):
    """Attribute values recorded for one site."""
    db = SessionLocal()
    try:
        site = db.query(Site).filter(Site.id == site_id).first()
        if not site:
            raise HTTPException(status_code=404, detail="site_id no existe.")
        allowed = accessible_project_ids(db, user)
        if allowed is not None and site.project_id not in allowed:
            raise HTTPException(status_code=404, detail="site_id no existe.")
        return (
            db.query(SiteAttributeValue)
            .filter(SiteAttributeValue.site_id == site_id)
            .order_by(SiteAttributeValue.effective_date.asc().nullsfirst())
            .all()
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post(
    "/site-attribute-values",
    response_model=AttributeValueRead,
    status_code=201,
    dependencies=[Depends(require_editor)],
)
async def create_site_attribute_value(payload: SiteAttributeValueCreate = Body(...)):
    """Record an attribute value for a site."""
    db = SessionLocal()
    try:
        if db.query(Site).filter(Site.id == payload.site_id).first() is None:
            raise HTTPException(status_code=404, detail="site_id no existe.")
        if db.query(SiteAttribute).filter(SiteAttribute.id == payload.site_attribute_id).first() is None:
            raise HTTPException(status_code=404, detail="site_attribute_id no existe.")
        return eav.create_attribute_value(
            db,
            SiteAttributeValue,
            payload,
            {"site_id": payload.site_id, "site_attribute_id": payload.site_attribute_id},
        )
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()
