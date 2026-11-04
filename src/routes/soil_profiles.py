"""Soil profile endpoints — RF10 / RF11 / RF12.

Create soil profiles (minimal entity) and manage their EAV attributes/values.
Reads are Viewer+, writes require Editor/Admin.
"""

from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import (
    Project,
    SamplingEvent,
    Site,
    SoilProfile,
    SoilProfileAttribute,
    SoilProfileAttributeValue,
)

from schemas.eav import (
    AttributeDefinitionCreate,
    AttributeDefinitionRead,
    AttributeValueRead,
    SoilProfileAttributeValueCreate,
    SoilProfileCreate,
    SoilProfileRead,
)
from services import eav
from services.etl import normalize_code
from dependencies.auth import CurrentUser, get_current_user, require_editor, require_database_editor
from dependencies.access import accessible_project_ids

router = APIRouter(tags=["Soil profiles"])


# ── Soil profiles (RF12) ─────────────────────────────────────────────────────

@router.get("/soil-profiles", response_model=List[SoilProfileRead])
async def list_soil_profiles(
    project_id: Optional[int] = Query(None),
    skip: int = 0,
    limit: int = 100,
    user: CurrentUser = Depends(get_current_user),
):
    """Soil profiles, optionally filtered by project. Paginated."""
    db = SessionLocal()
    try:
        query = db.query(SoilProfile)
        if project_id is not None:
            query = query.filter(SoilProfile.project_id == project_id)
        allowed = accessible_project_ids(db, user)
        if allowed is not None:
            query = query.filter(SoilProfile.project_id.in_(allowed))
        return query.order_by(SoilProfile.profile_code.asc()).offset(skip).limit(limit).all()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/soil-profiles/{profile_id}", response_model=SoilProfileRead)
async def get_soil_profile(profile_id: int, user: CurrentUser = Depends(get_current_user)):
    """One soil profile by id."""
    db = SessionLocal()
    try:
        profile = db.query(SoilProfile).filter(SoilProfile.id == profile_id).first()
        if not profile:
            raise HTTPException(status_code=404, detail="Perfil no encontrado.")
        allowed = accessible_project_ids(db, user)
        if allowed is not None and profile.project_id not in allowed:
            raise HTTPException(status_code=404, detail="Perfil no encontrado.")
        return profile
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post(
    "/soil-profiles",
    response_model=SoilProfileRead,
    status_code=201,
    dependencies=[Depends(require_editor)],
)
async def create_soil_profile(payload: SoilProfileCreate = Body(...)):
    """Create a soil profile for a project."""
    db = SessionLocal()
    try:
        code = normalize_code(payload.profile_code)
        if not code:
            raise HTTPException(status_code=400, detail="profile_code requerido.")
        if db.query(Project).filter(Project.id == payload.project_id).first() is None:
            raise HTTPException(status_code=404, detail="project_id no existe.")
        if payload.site_id is not None and db.query(Site).filter(Site.id == payload.site_id).first() is None:
            raise HTTPException(status_code=404, detail="site_id no existe.")
        if payload.event_id is not None and db.query(SamplingEvent).filter(SamplingEvent.id == payload.event_id).first() is None:
            raise HTTPException(status_code=404, detail="event_id no existe.")
        existing = (
            db.query(SoilProfile)
            .filter(SoilProfile.project_id == payload.project_id, SoilProfile.profile_code == code)
            .first()
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"Ya existe un perfil '{code}' en ese proyecto.")

        profile = SoilProfile(
            project_id=payload.project_id,
            profile_code=code,
            site_id=payload.site_id,
            event_id=payload.event_id,
            notes=payload.notes,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        return profile
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


# ── Soil profile attributes (RF10) ───────────────────────────────────────────

@router.get("/soil-profile-attributes", response_model=List[AttributeDefinitionRead])
async def list_soil_profile_attributes():
    """Attribute definitions that can be recorded about a soil profile."""
    db = SessionLocal()
    try:
        rows = db.query(SoilProfileAttribute).order_by(SoilProfileAttribute.name.asc()).all()
        return [eav.definition_read(row, "soil_profile_attribute_code") for row in rows]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post(
    "/soil-profile-attributes",
    response_model=AttributeDefinitionRead,
    status_code=201,
    dependencies=[Depends(require_database_editor)],
)
async def create_soil_profile_attribute(payload: AttributeDefinitionCreate = Body(...)):
    """Define a new soil-profile attribute. Extends the catalogue, so database_editor only."""
    db = SessionLocal()
    try:
        obj = eav.create_attribute_definition(
            db, SoilProfileAttribute, "soil_profile_attribute_code", payload
        )
        return eav.definition_read(obj, "soil_profile_attribute_code")
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


# ── Soil profile attribute values (RF11) ─────────────────────────────────────

@router.get("/soil-profile-attribute-values", response_model=List[AttributeValueRead])
async def list_soil_profile_attribute_values(
    soil_profile_id: int = Query(..., description="Soil profile ID"),
    user: CurrentUser = Depends(get_current_user),
):
    """Attribute values recorded for one soil profile."""
    db = SessionLocal()
    try:
        profile = db.query(SoilProfile).filter(SoilProfile.id == soil_profile_id).first()
        if not profile:
            raise HTTPException(status_code=404, detail="soil_profile_id no existe.")
        allowed = accessible_project_ids(db, user)
        if allowed is not None and profile.project_id not in allowed:
            raise HTTPException(status_code=404, detail="soil_profile_id no existe.")
        return (
            db.query(SoilProfileAttributeValue)
            .filter(SoilProfileAttributeValue.soil_profile_id == soil_profile_id)
            .order_by(SoilProfileAttributeValue.effective_date.asc().nullsfirst())
            .all()
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post(
    "/soil-profile-attribute-values",
    response_model=AttributeValueRead,
    status_code=201,
    dependencies=[Depends(require_editor)],
)
async def create_soil_profile_attribute_value(payload: SoilProfileAttributeValueCreate = Body(...)):
    """Record an attribute value for a soil profile."""
    db = SessionLocal()
    try:
        if db.query(SoilProfile).filter(SoilProfile.id == payload.soil_profile_id).first() is None:
            raise HTTPException(status_code=404, detail="soil_profile_id no existe.")
        if db.query(SoilProfileAttribute).filter(SoilProfileAttribute.id == payload.soil_profile_attribute_id).first() is None:
            raise HTTPException(status_code=404, detail="soil_profile_attribute_id no existe.")
        return eav.create_attribute_value(
            db,
            SoilProfileAttributeValue,
            payload,
            {
                "soil_profile_id": payload.soil_profile_id,
                "soil_profile_attribute_id": payload.soil_profile_attribute_id,
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()
