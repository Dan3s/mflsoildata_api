from fastapi import APIRouter, HTTPException, Query
from typing import List
from sqlalchemy.orm import Session
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models.site import Site as SiteModel
from schemas.sites import SiteRead, SiteCreate, SiteUpdate
from sqlalchemy import func

router = APIRouter(prefix="/sites", tags=["Sites"])


@router.get("/", response_model=List[SiteRead])
async def get_sites(
    project_id: int = Query(..., description="Project ID"),
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
):
    """
    Get all sites for a project
    """
    db = SessionLocal()
    try:
        sites = (
            db.query(SiteModel)
            .filter(SiteModel.project_id == project_id)
            .offset(skip)
            .limit(limit)
            .all()
        )
        return sites
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.get("/{site_id}", response_model=SiteRead)
async def get_site(site_id: int):
    """
    Get a specific site by ID
    """
    db = SessionLocal()
    try:
        site = db.query(SiteModel).filter(SiteModel.id == site_id).first()
        if not site:
            raise HTTPException(status_code=404, detail="Site not found")
        return site
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.post("/", response_model=SiteRead)
async def create_site(site: SiteCreate):
    """
    Create a new site with coordinates
    """
    db = SessionLocal()
    try:
        db_site = SiteModel(
            project_id=site.project_id,
            site_code=site.site_code,
            country=site.country,
            admin_1=site.admin_1,
            admin_2=site.admin_2,
            admin_3=site.admin_3,
            altitude_m=site.altitude_m,
            geom=func.ST_SetSRID(
                func.ST_MakePoint(site.longitude, site.latitude), 4326
            ),
        )
        db.add(db_site)
        db.commit()
        db.refresh(db_site)
        return db_site
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.put("/{site_id}", response_model=SiteRead)
async def update_site(site_id: int, site: SiteUpdate):
    """
    Update a site
    """
    db = SessionLocal()
    try:
        db_site = db.query(SiteModel).filter(SiteModel.id == site_id).first()
        if not db_site:
            raise HTTPException(status_code=404, detail="Site not found")
        
        update_data = site.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_site, field, value)
        
        db.commit()
        db.refresh(db_site)
        return db_site
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
