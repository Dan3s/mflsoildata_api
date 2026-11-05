from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import Site, SamplingEvent
from mflsoildata_orm.models.measurement import Measurement as MeasurementModel
from mflsoildata_orm.models.sample import Sample as SampleModel
from schemas.measurements import MeasurementRead, MeasurementCreate
from dependencies.auth import CurrentUser, get_current_user, require_editor
from dependencies.access import accessible_project_ids

router = APIRouter(prefix="/measurements", tags=["Measurements"])


def _measurement_to_read(measurement: MeasurementModel) -> dict:
    event_date = None
    if measurement.sample and measurement.sample.event:
        event_date = measurement.sample.event.event_date

    return {
        "id": measurement.id,
        "measurement_code": measurement.measurement_code,
        "sample_id": measurement.sample_id,
        "event_date": event_date,
        "variable_id": measurement.variable_id,
        "lab_batch_id": measurement.lab_batch_id,
        "unit_id": measurement.unit_id,
        "value_numeric": measurement.value_numeric,
        "value_text": measurement.value_text,
        "value_boolean": measurement.value_boolean,
        "value_date": measurement.value_date,
        "qualifier_result": measurement.qualifier_result,
        "created_at": measurement.created_at,
        "updated_at": measurement.updated_at,
    }


@router.get("/", response_model=List[MeasurementRead])
async def get_measurements(
    sample_id: int = Query(None, description="Filter by Sample ID"),
    variable_id: int = Query(None, description="Filter by Variable ID"),
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Get measurements with optional filters
    """
    db = SessionLocal()
    try:
        query = (
            db.query(MeasurementModel)
            .options(joinedload(MeasurementModel.sample).joinedload(SampleModel.event))
            .join(SampleModel, MeasurementModel.sample_id == SampleModel.id)
            .outerjoin(Site, SampleModel.site_id == Site.id)
            .outerjoin(SamplingEvent, SampleModel.event_id == SamplingEvent.id)
        )

        if sample_id:
            query = query.filter(MeasurementModel.sample_id == sample_id)
        if variable_id:
            query = query.filter(MeasurementModel.variable_id == variable_id)

        allowed = accessible_project_ids(db, user)
        if allowed is not None:
            query = query.filter(
                or_(Site.project_id.in_(allowed), SamplingEvent.project_id.in_(allowed))
            )

        measurements = query.offset(skip).limit(limit).all()
        return [_measurement_to_read(measurement) for measurement in measurements]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.get("/{measurement_id}", response_model=MeasurementRead)
async def get_measurement(measurement_id: int, user: CurrentUser = Depends(get_current_user)):
    """
    Get a specific measurement by ID
    """
    db = SessionLocal()
    try:
        measurement = (
            db.query(MeasurementModel)
            .options(joinedload(MeasurementModel.sample).joinedload(SampleModel.event))
            .filter(MeasurementModel.id == measurement_id)
            .first()
        )
        if not measurement:
            raise HTTPException(status_code=404, detail="Measurement not found")
        allowed = accessible_project_ids(db, user)
        if allowed is not None:
            sample = measurement.sample
            candidate_ids = {
                pid
                for pid in (
                    sample.site.project_id if sample and sample.site else None,
                    sample.event.project_id if sample and sample.event else None,
                )
                if pid is not None
            }
            if not candidate_ids & allowed:
                raise HTTPException(status_code=404, detail="Measurement not found")
        return _measurement_to_read(measurement)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.post("/", response_model=MeasurementRead, dependencies=[Depends(require_editor)])
async def create_measurement(measurement: MeasurementCreate):
    """
    Create a new measurement
    """
    db = SessionLocal()
    try:
        db_measurement = MeasurementModel(**measurement.model_dump())
        db.add(db_measurement)
        db.commit()
        db.refresh(db_measurement)
        db_measurement = (
            db.query(MeasurementModel)
            .options(joinedload(MeasurementModel.sample).joinedload(SampleModel.event))
            .filter(MeasurementModel.id == db_measurement.id)
            .first()
        )
        return _measurement_to_read(db_measurement)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
