from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
from sqlalchemy import or_
from sqlalchemy.orm import Session
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import Site, SamplingEvent
from mflsoildata_orm.models.sample import Sample as SampleModel
from schemas.samples import SampleRead, SampleCreate, SampleUpdate
from dependencies.auth import CurrentUser, get_current_user, require_editor
from dependencies.access import accessible_project_ids

router = APIRouter(prefix="/samples", tags=["Samples"])


@router.get("/", response_model=List[SampleRead])
async def get_samples(
    event_id: int = Query(None, description="Filter by Event ID"),
    site_id: int = Query(None, description="Filter by Site ID"),
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Get samples with optional filters
    """
    db = SessionLocal()
    try:
        # A sample's project lives on whichever of site/event it's linked to
        # (both are nullable), so both must be joined to apply access control.
        query = (
            db.query(SampleModel)
            .outerjoin(Site, SampleModel.site_id == Site.id)
            .outerjoin(SamplingEvent, SampleModel.event_id == SamplingEvent.id)
        )

        if event_id:
            query = query.filter(SampleModel.event_id == event_id)
        if site_id:
            query = query.filter(SampleModel.site_id == site_id)

        allowed = accessible_project_ids(db, user)
        if allowed is not None:
            query = query.filter(
                or_(Site.project_id.in_(allowed), SamplingEvent.project_id.in_(allowed))
            )

        samples = query.offset(skip).limit(limit).all()
        return samples
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.get("/{sample_id}", response_model=SampleRead)
async def get_sample(sample_id: int, user: CurrentUser = Depends(get_current_user)):
    """
    Get a specific sample by ID
    """
    db = SessionLocal()
    try:
        sample = db.query(SampleModel).filter(SampleModel.id == sample_id).first()
        if not sample:
            raise HTTPException(status_code=404, detail="Sample not found")
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
                raise HTTPException(status_code=404, detail="Sample not found")
        return sample
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.post("/", response_model=SampleRead, dependencies=[Depends(require_editor)])
async def create_sample(sample: SampleCreate):
    """
    Create a new sample
    """
    db = SessionLocal()
    try:
        db_sample = SampleModel(**sample.model_dump())
        db.add(db_sample)
        db.commit()
        db.refresh(db_sample)
        return db_sample
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.put("/{sample_id}", response_model=SampleRead, dependencies=[Depends(require_editor)])
async def update_sample(sample_id: int, sample: SampleUpdate):
    """
    Update a sample
    """
    db = SessionLocal()
    try:
        db_sample = db.query(SampleModel).filter(SampleModel.id == sample_id).first()
        if not db_sample:
            raise HTTPException(status_code=404, detail="Sample not found")
        
        update_data = sample.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_sample, field, value)
        
        db.commit()
        db.refresh(db_sample)
        return db_sample
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
