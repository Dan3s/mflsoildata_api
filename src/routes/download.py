"""Data download endpoints (descarga de datos).

``estimate`` and ``export`` share the exact same query builder so the estimated
record count matches what is actually downloaded.
"""

import io
import re
from csv import DictWriter
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import aliased

from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import (
    DownloadLog,
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
from dependencies.auth import CurrentUser, get_current_user, get_optional_user, require_admin
from dependencies.access import accessible_project_ids, narrow, project_ids_by_institution
from schemas.download import DownloadEstimate, DownloadFacets, RecentDownload

router = APIRouter(prefix="/download", tags=["Download"])

EXPORT_FIELDS = [
    "project_code",
    "project_name",
    "site_code",
    "country",
    "admin_1",
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

# Columns that many datasets simply do not have. When nothing in the file fills one in,
# it is left out rather than shipped as an empty column that looks like a defect.
OPTIONAL_FIELDS = {"country", "admin_1", "event_date", "qualifier", "laboratory"}


def _export_fields(records) -> List[str]:
    return [
        field
        for field in EXPORT_FIELDS
        if field not in OPTIONAL_FIELDS or any(rec.get(field) not in (None, "") for rec in records)
    ]


def _export_filename(records, matrix: Optional[str], ext: str) -> str:
    """``mfl_<project>_<matrix>_<date>.<ext>`` so different downloads do not overwrite each other."""
    codes = sorted({rec["project_code"] for rec in records if rec.get("project_code")})
    if len(codes) == 1:
        scope = codes[0]
    elif codes:
        scope = f"{len(codes)}-projects"
    else:
        scope = "no-data"
    parts = ["mfl", scope, matrix, datetime.utcnow().strftime("%Y%m%d")]
    name = "_".join(re.sub(r"[^A-Za-z0-9-]+", "-", str(p)).strip("-") for p in parts if p)
    return f"{name}.{ext}"


def _filtered_query(
    db,
    project_ids: Optional[List[int]],
    site_ids: Optional[List[int]],
    variable_ids: Optional[List[int]],
    date_from: Optional[date],
    date_to: Optional[date],
    matrix: Optional[str],
    country: Optional[str] = None,
):
    measurement_unit = aliased(Unit)
    default_unit = aliased(Unit)

    query = (
        db.query(
            Project.code.label("project_code"),
            Project.name.label("project_name"),
            Project.id.label("project_id"),
            Variable.id.label("variable_id"),
            Site.id.label("site_id"),
            Site.site_code.label("site_code"),
            Site.country.label("country"),
            Site.admin_1.label("admin_1"),
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
    if country:
        query = query.filter(Site.country == country)
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
    country: Optional[str] = Query(None),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """Real record/sample/site counts for the given filters (matches /export)."""
    db = SessionLocal()
    try:
        query = _filtered_query(db, project_ids, site_ids, variable_ids, date_from, date_to, matrix, country)
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        if allowed is not None:
            query = query.filter(Project.id.in_(allowed))
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


@router.get("/facets", response_model=DownloadFacets)
async def facets(
    project_ids: List[int] = Query(default=[]),
    site_ids: List[int] = Query(default=[]),
    variable_ids: List[int] = Query(default=[]),
    matrix: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """Options that have data under the current choices (cascading filters).

    - ``project_ids`` (output): projects with data under institution/country/matrix only,
      ignoring which projects are already ticked, so the list can shrink but never traps.
    - ``variable_ids``: variables measured within the chosen projects/sites.
    - ``date_min``/``date_max``: the real time span of the chosen projects/sites/variables.
    """
    db = SessionLocal()
    try:
        allowed = accessible_project_ids(db, user)
        allowed = narrow(allowed, project_ids_by_institution(db, institution))

        def scoped(pids, sids, vids):
            query = _filtered_query(db, pids, sids, vids, None, None, matrix, country)
            if allowed is not None:
                query = query.filter(Project.id.in_(allowed))
            return query.subquery()

        in_scope = scoped(None, None, None)
        projects = sorted(r[0] for r in db.query(in_scope.c.project_id).distinct().all())

        chosen = scoped(project_ids, site_ids, None)
        counts = {
            vid: n
            for vid, n in db.query(chosen.c.variable_id, func.count()).group_by(chosen.c.variable_id).all()
        }
        variables = sorted(counts)

        span = scoped(project_ids, site_ids, variable_ids)
        date_min, date_max = db.query(func.min(span.c.event_date), func.max(span.c.event_date)).one()

        return DownloadFacets(
            project_ids=projects,
            variable_ids=variables,
            variable_counts=counts,
            date_min=date_min,
            date_max=date_max,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


def _log_download(db, user: Optional[CurrentUser], filters: dict, fmt: str, record_count: int) -> None:
    """Best-effort audit entry (RF32); never let logging break the download."""
    try:
        db.add(
            DownloadLog(
                user_sub=user.sub if user else None,
                user_email=user.email if user else None,
                username=user.username if user else None,
                organization=getattr(user, "organization", None) if user else None,
                country=getattr(user, "country", None) if user else None,
                format=fmt,
                record_count=record_count,
                filters=filters,
            )
        )
        db.commit()
    except Exception:
        db.rollback()


@router.get("/export")
async def export(
    project_ids: List[int] = Query(default=[]),
    site_ids: List[int] = Query(default=[]),
    variable_ids: List[int] = Query(default=[]),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    matrix: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    format: str = Query("xlsx"),
    include_metadata: bool = Query(False),
    user: Optional[CurrentUser] = Depends(get_optional_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """Export the filtered measurements as a binary xlsx/csv download."""
    fmt = format.lower()
    if fmt not in {"xlsx", "csv"}:
        raise HTTPException(status_code=400, detail="format must be 'xlsx' or 'csv'")

    db = SessionLocal()
    try:
        query = _filtered_query(db, project_ids, site_ids, variable_ids, date_from, date_to, matrix, country)
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        if allowed is not None:
            query = query.filter(Project.id.in_(allowed))
        rows = query.order_by(Project.code, Site.site_code, Sample.sample_code).all()

        records = []
        for row in rows:
            records.append(
                {
                    "project_code": row.project_code,
                    "project_name": row.project_name,
                    "site_code": row.site_code,
                    "country": row.country or "",
                    "admin_1": row.admin_1 or "",
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

        applied = {
            "institution": institution,
            "country": country,
            "matrix": matrix,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
        }
        if fmt == "csv":
            content, media_type, ext = _export_csv(records, include_metadata, applied)
        else:
            content, media_type, ext = _export_xlsx(records, include_metadata, applied)

        _log_download(
            db,
            user,
            {
                "project_ids": project_ids,
                "site_ids": site_ids,
                "variable_ids": variable_ids,
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
                "matrix": matrix,
                "country": country,
                "include_metadata": include_metadata,
            },
            fmt,
            len(records),
        )

        filename = _export_filename(records, matrix, ext)
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


def _metadata_rows(record_count: int, applied: Optional[dict] = None) -> List[List[str]]:
    rows = [
        ["Exportado", datetime.utcnow().isoformat()],
        ["Registros", str(record_count)],
        ["Fuente", "MFL Soil Data API"],
    ]
    # Filters that narrowed the download but have no column of their own (institution)
    # or that explain an empty one (dates), so the file says what it is a slice of.
    labels = {
        "institution": "Institución",
        "country": "País",
        "matrix": "Matriz",
        "date_from": "Fecha desde",
        "date_to": "Fecha hasta",
    }
    for key, label in labels.items():
        value = (applied or {}).get(key)
        if value:
            rows.append([f"Filtro · {label}", str(value)])
    return rows


def _export_csv(records, include_metadata, applied=None):
    buffer = io.StringIO()
    if include_metadata:
        for key, value in _metadata_rows(len(records), applied):
            buffer.write(f"# {key}: {value}\n")
    writer = DictWriter(buffer, fieldnames=_export_fields(records), extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        writer.writerow(rec)
    return buffer.getvalue().encode("utf-8-sig"), "text/csv", "csv"


def _export_xlsx(records, include_metadata, applied=None):
    from openpyxl import Workbook

    fields = _export_fields(records)
    wb = Workbook()
    ws = wb.active
    ws.title = "Datos"
    ws.append(fields)
    for rec in records:
        ws.append([rec.get(field, "") for field in fields])

    if include_metadata:
        meta = wb.create_sheet("Metadatos")
        for key, value in _metadata_rows(len(records), applied):
            meta.append([key, value])

    buffer = io.BytesIO()
    wb.save(buffer)
    return (
        buffer.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    )


@router.get("/audit", response_model=List[RecentDownload])
async def download_audit(
    limit: int = Query(20, ge=1, le=200),
    _admin: CurrentUser = Depends(require_admin),
):
    """Download audit log (RF32) — ADMIN ONLY: all users' recent exports.

    Ordered newest first. A human-readable filename is synthesized from the
    timestamp + format (the export file itself is not stored).
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(DownloadLog)
            .order_by(DownloadLog.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            RecentDownload(
                id=row.id,
                filename=f"export_{row.created_at.strftime('%Y%m%d')}.{row.format}",
                created_at=row.created_at,
                format=row.format,
                record_count=row.record_count,
                username=row.username,
                user_email=row.user_email,
                organization=row.organization,
                country=row.country,
            )
            for row in rows
        ]
    finally:
        db.close()
