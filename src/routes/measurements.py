from fastapi import APIRouter, HTTPException, Query
from typing import List
from sqlalchemy.orm import Session
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models.measurement import Measurement as MeasurementModel
from schemas.measurements import MeasurementRead, MeasurementCreate

router = APIRouter(prefix="/measurements", tags=["Measurements"])


@router.get("/", response_model=List[MeasurementRead])
async def get_measurements(
    sample_id: int = Query(None, description="Filter by Sample ID"),
    variable_id: int = Query(None, description="Filter by Variable ID"),
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
):
    """
    Get measurements with optional filters
    """
    db = SessionLocal()
    try:
        query = db.query(MeasurementModel)
        
        if sample_id:
            query = query.filter(MeasurementModel.sample_id == sample_id)
        if variable_id:
            query = query.filter(MeasurementModel.variable_id == variable_id)
        
        measurements = query.offset(skip).limit(limit).all()
        return measurements
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.get("/{measurement_id}", response_model=MeasurementRead)
async def get_measurement(measurement_id: int):
    """
    Get a specific measurement by ID
    """
    db = SessionLocal()
    try:
        measurement = db.query(MeasurementModel).filter(MeasurementModel.id == measurement_id).first()
        if not measurement:
            raise HTTPException(status_code=404, detail="Measurement not found")
        return measurement
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.post("/", response_model=MeasurementRead)
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
        return db_measurement
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
