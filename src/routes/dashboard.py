from csv import DictWriter
from datetime import date
from io import StringIO
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import or_
from sqlalchemy.orm import aliased

from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import (
    Lab,
    LabBatch,
    Measurement,
    Project,
    Sample,
    SampleType,
    SamplingEvent,
    Site,
    Unit,
    Variable,
)
from schemas.lookups import DashboardRecord, DashboardRecordsResponse, DashboardSummary

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


def _base_records_query(db):
    measurement_unit = aliased(Unit)
    default_unit = aliased(Unit)

    return (
        db.query(
            Measurement.id.label("measurement_id"),
            Measurement.measurement_code.label("measurement_code"),
            Project.id.label("project_id"),
            Project.code.label("project_code"),
            Project.name.label("project_name"),
            Site.id.label("site_id"),
            Site.site_code.label("site_code"),
            Sample.id.label("sample_id"),
            Sample.sample_code.label("sample_code"),
            SampleType.id.label("sample_type_id"),
            SampleType.name.label("sample_type"),
            Variable.id.label("variable_id"),
            Variable.variable_code.label("variable_code"),
            Variable.name.label("variable_name"),
            Variable.matrix.label("matrix"),
            Measurement.value_numeric.label("value_numeric"),
            Measurement.value_text.label("value_text"),
            Measurement.value_boolean.label("value_boolean"),
            Measurement.value_date.label("value_date"),
            Measurement.qualifier_result.label("qualifier_result"),
            Measurement.notes.label("measurement_notes"),
            measurement_unit.symbol.label("measurement_unit_symbol"),
            default_unit.symbol.label("default_unit_symbol"),
            Lab.name.label("laboratory"),
            LabBatch.lab_batch_code.label("lab_batch_code"),
            SamplingEvent.event_date.label("event_date"),
        )
        .select_from(Measurement)
        .join(Sample, Measurement.sample_id == Sample.id)
        .join(SampleType, Sample.sample_type_id == SampleType.id)
        .join(Variable, Measurement.variable_id == Variable.id)
        .join(SamplingEvent, Sample.event_id == SamplingEvent.id)
        .join(Project, SamplingEvent.project_id == Project.id)
        .outerjoin(Site, Sample.site_id == Site.id)
        .outerjoin(LabBatch, Measurement.lab_batch_id == LabBatch.id)
        .outerjoin(Lab, LabBatch.lab_id == Lab.id)
        .outerjoin(measurement_unit, Measurement.unit_id == measurement_unit.id)
        .outerjoin(default_unit, Variable.default_unit_id == default_unit.id)
    )


def _apply_record_filters(
    query,
    project_id: Optional[int] = None,
    site_id: Optional[int] = None,
    sample_type_id: Optional[int] = None,
    matrix: Optional[str] = None,
    variable_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    search: Optional[str] = None,
):
    if project_id is not None:
        query = query.filter(Project.id == project_id)
    if site_id is not None:
        query = query.filter(Site.id == site_id)
    if sample_type_id is not None:
        query = query.filter(SampleType.id == sample_type_id)
    if matrix is not None:
        query = query.filter(Variable.matrix == matrix)
    if variable_id is not None:
        query = query.filter(Variable.id == variable_id)
    if date_from is not None:
        query = query.filter(SamplingEvent.event_date >= date_from)
    if date_to is not None:
        query = query.filter(SamplingEvent.event_date <= date_to)
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                Project.name.ilike(pattern),
                Project.code.ilike(pattern),
                Site.site_code.ilike(pattern),
                Sample.sample_code.ilike(pattern),
                SampleType.name.ilike(pattern),
                Variable.name.ilike(pattern),
                Variable.variable_code.ilike(pattern),
                Measurement.measurement_code.ilike(pattern),
                Lab.name.ilike(pattern),
                SamplingEvent.activity.ilike(pattern),
            )
        )

    return query


@router.get("/summary", response_model=DashboardSummary)
async def get_summary():
    db = SessionLocal()
    try:
        return DashboardSummary(
            projects=db.query(Project).count(),
            sites=db.query(Site).count(),
            samples=db.query(Sample).count(),
            variables=db.query(Variable).count(),
            measurements=db.query(Measurement).count(),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/records", response_model=DashboardRecordsResponse)
async def get_records(
    project_id: Optional[int] = Query(None),
    site_id: Optional[int] = Query(None),
    sample_type_id: Optional[int] = Query(None),
    matrix: Optional[str] = Query(None),
    variable_id: Optional[int] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    search: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=200),
):
    db = SessionLocal()
    try:
        query = _base_records_query(db)
        query = _apply_record_filters(query, project_id, site_id, sample_type_id, matrix, variable_id, date_from, date_to, search)

        total = query.count()
        rows = query.order_by(Measurement.created_at.desc()).offset(skip).limit(limit).all()

        items = []
        for row in rows:
            value = row.value_numeric
            if value is None:
                value = row.value_text
            if value is None:
                value = row.value_boolean
            if value is None and row.value_date is not None:
                value = row.value_date.isoformat()

            unit = row.measurement_unit_symbol or row.default_unit_symbol
            record_date = row.event_date
            if record_date is None and row.value_date is not None:
                record_date = row.value_date

            status = "Pendiente"
            if row.measurement_notes and isinstance(row.measurement_notes, dict):
                note_status = row.measurement_notes.get("status")
                if note_status:
                    status = str(note_status)
            if row.qualifier_result:
                status = row.qualifier_result

            items.append(
                DashboardRecord(
                    measurement_id=row.measurement_id,
                    measurement_code=row.measurement_code,
                    project_id=row.project_id,
                    project_code=row.project_code,
                    project_name=row.project_name,
                    site_id=row.site_id,
                    site_code=row.site_code,
                    sample_id=row.sample_id,
                    sample_code=row.sample_code,
                    sample_type_id=row.sample_type_id,
                    sample_type=row.sample_type,
                    matrix=row.matrix,
                    variable_id=row.variable_id,
                    variable_code=row.variable_code,
                    variable_name=row.variable_name,
                    value=value,
                    unit=unit,
                    date=record_date,
                    laboratory=row.laboratory,
                    status=status,
                )
            )

        return DashboardRecordsResponse(items=items, total=total, skip=skip, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/export")
async def export_records(
    project_id: Optional[int] = Query(None),
    site_id: Optional[int] = Query(None),
    sample_type_id: Optional[int] = Query(None),
    matrix: Optional[str] = Query(None),
    variable_id: Optional[int] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    search: Optional[str] = Query(None),
):
    db = SessionLocal()
    try:
        query = _base_records_query(db)
        query = _apply_record_filters(query, project_id, site_id, sample_type_id, matrix, variable_id, date_from, date_to, search)
        rows = query.order_by(Measurement.created_at.desc()).all()

        buffer = StringIO()
        writer = DictWriter(
            buffer,
            fieldnames=[
                "measurement_id",
                "measurement_code",
                "project_id",
                "project_code",
                "project_name",
                "site_id",
                "site_code",
                "sample_id",
                "sample_code",
                "sample_type_id",
                "sample_type",
                "matrix",
                "variable_id",
                "variable_code",
                "variable_name",
                "value",
                "unit",
                "date",
                "laboratory",
                "status",
            ],
        )
        writer.writeheader()
        for row in rows:
            value = row.value_numeric
            if value is None:
                value = row.value_text
            if value is None:
                value = row.value_boolean
            if value is None and row.value_date is not None:
                value = row.value_date.isoformat()

            writer.writerow(
                {
                    "measurement_id": row.measurement_id,
                    "measurement_code": row.measurement_code,
                    "project_id": row.project_id,
                    "project_code": row.project_code,
                    "project_name": row.project_name,
                    "site_id": row.site_id,
                    "site_code": row.site_code,
                    "sample_id": row.sample_id,
                    "sample_code": row.sample_code,
                    "sample_type_id": row.sample_type_id,
                    "sample_type": row.sample_type,
                    "matrix": row.matrix,
                    "variable_id": row.variable_id,
                    "variable_code": row.variable_code,
                    "variable_name": row.variable_name,
                    "value": value,
                    "unit": row.measurement_unit_symbol or row.default_unit_symbol,
                    "date": row.event_date.isoformat() if row.event_date else (row.value_date.isoformat() if row.value_date else ""),
                    "laboratory": row.laboratory,
                    "status": row.qualifier_result or (
                        row.measurement_notes.get("status") if row.measurement_notes and isinstance(row.measurement_notes, dict) else "Pendiente"
                    ),
                }
            )

        csv_content = buffer.getvalue()
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="dashboard_records.csv"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()
