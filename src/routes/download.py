"""Data download endpoints (descarga de datos).

``estimate`` and ``export`` share the exact same query builder so the estimated
record count matches what is actually downloaded.
"""

import io
from csv import DictWriter
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func
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
from schemas.download import DownloadEstimate, RecentDownload

router = APIRouter(prefix="/download", tags=["Download"])

EXPORT_FIELDS = [
    "project_code",
    "project_name",
    "site_code",
    "latitude",
    "longitude",
    "event_date",
    "sample_code",
    "sample_type",
    "variable_code",
    "variable_name",
    "matrix",
    "value",
    "unit",
    "qualifier",
    "laboratory",
]


def _filtered_query(
    db,
    project_ids: Optional[List[int]],
    site_ids: Optional[List[int]],
    variable_ids: Optional[List[int]],
    date_from: Optional[date],
    date_to: Optional[date],
    matrix: Optional[str],
):
    measurement_unit = aliased(Unit)
    default_unit = aliased(Unit)

    query = (
        db.query(
            Project.code.label("project_code"),
            Project.name.label("project_name"),
            Site.id.label("site_id"),
            Site.site_code.label("site_code"),
            func.ST_Y(Site.geom).label("latitude"),
            func.ST_X(Site.geom).label("longitude"),
            SamplingEvent.event_date.label("event_date"),
            Sample.id.label("sample_id"),
            Sample.sample_code.label("sample_code"),
            SampleType.name.label("sample_type"),
            Variable.variable_code.label("variable_code"),
            Variable.name.label("variable_name"),
            Variable.matrix.label("matrix"),
            Measurement.id.label("measurement_id"),
            Measurement.value_numeric.label("value_numeric"),
            Measurement.value_text.label("value_text"),
            Measurement.value_boolean.label("value_boolean"),
            Measurement.value_date.label("value_date"),
            Measurement.qualifier_result.label("qualifier"),
            measurement_unit.symbol.label("measurement_unit_symbol"),
            default_unit.symbol.label("default_unit_symbol"),
            Lab.name.label("laboratory"),
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

    if project_ids:
        query = query.filter(Project.id.in_(project_ids))
    if site_ids:
        query = query.filter(Site.id.in_(site_ids))
    if variable_ids:
        query = query.filter(Variable.id.in_(variable_ids))
    if matrix:
        query = query.filter(Variable.matrix == matrix)
    if date_from is not None:
        query = query.filter(SamplingEvent.event_date >= date_from)
    if date_to is not None:
        query = query.filter(SamplingEvent.event_date <= date_to)

    return query


def _row_value(row):
    if row.value_numeric is not None:
        return row.value_numeric
    if row.value_text is not None:
        return row.value_text
    if row.value_boolean is not None:
        return row.value_boolean
    if row.value_date is not None:
        return row.value_date.isoformat()
    return None


@router.get("/estimate", response_model=DownloadEstimate)
async def estimate(
    project_ids: List[int] = Query(default=[]),
    site_ids: List[int] = Query(default=[]),
    variable_ids: List[int] = Query(default=[]),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    matrix: Optional[str] = Query(None),
):
    """Real record/sample/site counts for the given filters (matches /export)."""
    db = SessionLocal()
    try:
        query = _filtered_query(db, project_ids, site_ids, variable_ids, date_from, date_to, matrix)
        subq = query.subquery()
        record_count = db.query(func.count()).select_from(subq).scalar() or 0
        sample_count = db.query(func.count(func.distinct(subq.c.sample_id))).scalar() or 0
        site_count = db.query(func.count(func.distinct(subq.c.site_id))).scalar() or 0
        return DownloadEstimate(
            record_count=record_count,
            sample_count=sample_count,
            site_count=site_count,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/export")
async def export(
    project_ids: List[int] = Query(default=[]),
    site_ids: List[int] = Query(default=[]),
    variable_ids: List[int] = Query(default=[]),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    matrix: Optional[str] = Query(None),
    format: str = Query("xlsx"),
    include_metadata: bool = Query(False),
):
    """Export the filtered measurements as a binary xlsx/csv download."""
    fmt = format.lower()
    if fmt not in {"xlsx", "csv"}:
        raise HTTPException(status_code=400, detail="format must be 'xlsx' or 'csv'")

    db = SessionLocal()
    try:
        query = _filtered_query(db, project_ids, site_ids, variable_ids, date_from, date_to, matrix)
        rows = query.order_by(Project.code, Site.site_code, Sample.sample_code).all()

        records = []
        for row in rows:
            records.append(
                {
                    "project_code": row.project_code,
                    "project_name": row.project_name,
                    "site_code": row.site_code,
                    "latitude": float(row.latitude) if row.latitude is not None else "",
                    "longitude": float(row.longitude) if row.longitude is not None else "",
                    "event_date": row.event_date.isoformat() if row.event_date else "",
                    "sample_code": row.sample_code,
                    "sample_type": row.sample_type,
                    "variable_code": row.variable_code,
                    "variable_name": row.variable_name,
                    "matrix": row.matrix,
                    "value": _row_value(row),
                    "unit": row.measurement_unit_symbol or row.default_unit_symbol or "",
                    "qualifier": row.qualifier or "",
                    "laboratory": row.laboratory or "",
                }
            )

        stamp = datetime.utcnow().strftime("%Y%m%d")
        if fmt == "csv":
            content, media_type, ext = _export_csv(records, include_metadata)
        else:
            content, media_type, ext = _export_xlsx(records, include_metadata)

        filename = f"export_{stamp}.{ext}"
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


def _metadata_rows(record_count: int) -> List[List[str]]:
    return [
        ["Exportado", datetime.utcnow().isoformat()],
        ["Registros", str(record_count)],
        ["Fuente", "MFL Soil Data API"],
    ]


def _export_csv(records, include_metadata):
    buffer = io.StringIO()
    if include_metadata:
        for key, value in _metadata_rows(len(records)):
            buffer.write(f"# {key}: {value}\n")
    writer = DictWriter(buffer, fieldnames=EXPORT_FIELDS)
    writer.writeheader()
    for rec in records:
        writer.writerow(rec)
    return buffer.getvalue().encode("utf-8-sig"), "text/csv", "csv"


def _export_xlsx(records, include_metadata):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Datos"
    ws.append(EXPORT_FIELDS)
    for rec in records:
        ws.append([rec.get(field, "") for field in EXPORT_FIELDS])

    if include_metadata:
        meta = wb.create_sheet("Metadatos")
        for key, value in _metadata_rows(len(records)):
            meta.append([key, value])

    buffer = io.BytesIO()
    wb.save(buffer)
    return (
        buffer.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    )


@router.get("/recent", response_model=List[RecentDownload])
async def recent_downloads():
    """Recent downloads panel.

    There is no download-history persistence layer yet, so this returns an empty
    list. The frontend can hide the section until history is implemented.
    """
    return []
