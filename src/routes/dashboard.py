from csv import DictWriter
from datetime import date
from io import StringIO
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from dependencies.auth import CurrentUser, get_current_user
from dependencies.access import accessible_project_ids, narrow, project_ids_by_institution
from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import aliased

from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import (
    Lab,
    LabBatch,
    Measurement,
    Project,
    ProjectMetadata,
    Sample,
    SampleAttribute,
    SampleAttributeValue,
    SampleType,
    SamplingEvent,
    Site,
    Unit,
    Variable,
)

# Depth is stored as EAV on samples; accept the common code variants.
DEPTH_FROM_CODES = ("DEPTH_FROM_CM", "DEPTH_MIN_CM")
DEPTH_TO_CODES = ("DEPTH_TO_CM", "DEPTH_MAX_CM")

# Fixed dimension labels for the wide (explorer) dataset, per language (English primary).
_WIDE_DIM_LABELS = {
    "project_name": {"en": "Project", "es": "Proyecto"},
    "site_code": {"en": "Site", "es": "Sitio"},
    "sample_code": {"en": "Sample code", "es": "Código muestra"},
    "sample_type": {"en": "Sample type", "es": "Tipo de muestra"},
    "country": {"en": "Country", "es": "País"},
    "event_date": {"en": "Date", "es": "Fecha"},
    "depth_cm": {"en": "Depth (cm)", "es": "Profundidad (cm)"},
    "latitude": {"en": "Latitude", "es": "Latitud"},
    "longitude": {"en": "Longitude", "es": "Longitud"},
}


def _wide_label(key: str, lang: str) -> str:
    entry = _WIDE_DIM_LABELS.get(key, {})
    return entry.get(lang) or entry.get("en") or key
from schemas.lookups import DashboardRecord, DashboardRecordsResponse, DashboardSummary, PublicSummary

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

# Registered in main.py WITHOUT the app-wide auth dependency that wraps `router`
# below — this is the one dashboard endpoint meant for an anonymous visitor
# (the landing page teaser), so it needs its own, unauthenticated router.
public_router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


def _clean_coords(lat, lon):
    """Return (lat, lon) as floats, treating a placeholder (0, 0) point as missing."""
    la = float(lat) if lat is not None else None
    lo = float(lon) if lon is not None else None
    if la == 0 and lo == 0:
        return None, None
    return la, lo


def _depth_scalar(db, codes):
    """Correlated scalar subquery for a sample's depth (EAV) in cm, or NULL.

    Depth lives in ``sample_attribute_values`` under the code variants in
    ``DEPTH_FROM_CODES`` / ``DEPTH_TO_CODES``; this exposes it as a numeric
    column so charts can plot against depth without a separate query.
    """
    return (
        db.query(SampleAttributeValue.value_numeric)
        .join(SampleAttribute, SampleAttributeValue.sample_attribute_id == SampleAttribute.id)
        .filter(
            SampleAttributeValue.sample_id == Sample.id,
            SampleAttribute.sample_attribute_code.in_(codes),
            SampleAttributeValue.value_numeric.isnot(None),
        )
        .order_by(SampleAttributeValue.id)
        .limit(1)
        .correlate(Sample)
        .scalar_subquery()
    )


def _midpoint(depth_from, depth_to):
    """Representative depth (cm) for profile plots: midpoint, or whichever end exists."""
    lo = float(depth_from) if depth_from is not None else None
    hi = float(depth_to) if depth_to is not None else None
    if lo is not None and hi is not None:
        return (lo + hi) / 2
    return lo if lo is not None else hi


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
            func.ST_Y(func.ST_Centroid(Site.geom)).label("latitude"),
            func.ST_X(func.ST_Centroid(Site.geom)).label("longitude"),
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
            _depth_scalar(db, DEPTH_FROM_CODES).label("depth_from"),
            _depth_scalar(db, DEPTH_TO_CODES).label("depth_to"),
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
    country: Optional[str] = None,
    depth_from: Optional[float] = None,
    depth_to: Optional[float] = None,
):
    if project_id is not None:
        query = query.filter(Project.id == project_id)
    if site_id is not None:
        query = query.filter(Site.id == site_id)
    if country is not None:
        query = query.filter(Site.country == country)
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
    # Sampling depth (EAV): keep measurements whose sample has depth in range.
    if depth_from is not None:
        query = query.filter(
            exists().where(
                and_(
                    SampleAttributeValue.sample_id == Sample.id,
                    SampleAttributeValue.sample_attribute_id == SampleAttribute.id,
                    SampleAttribute.sample_attribute_code.in_(DEPTH_FROM_CODES),
                    SampleAttributeValue.value_numeric >= depth_from,
                )
            )
        )
    if depth_to is not None:
        query = query.filter(
            exists().where(
                and_(
                    SampleAttributeValue.sample_id == Sample.id,
                    SampleAttributeValue.sample_attribute_id == SampleAttribute.id,
                    SampleAttribute.sample_attribute_code.in_(DEPTH_TO_CODES),
                    SampleAttributeValue.value_numeric <= depth_to,
                )
            )
        )
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
async def get_summary(user: CurrentUser = Depends(get_current_user), institution: Optional[str] = Query(None)):
    """Counts of everything the caller may see. Filtered by the project access rules, so
    two users can legitimately get different numbers.
    """
    db = SessionLocal()
    try:
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        proj_q = db.query(Project)
        site_q = db.query(Site)
        sample_q = db.query(Sample).join(SamplingEvent, Sample.event_id == SamplingEvent.id)
        meas_q = (
            db.query(Measurement)
            .join(Sample, Measurement.sample_id == Sample.id)
            .join(SamplingEvent, Sample.event_id == SamplingEvent.id)
        )
        if allowed is not None:
            proj_q = proj_q.filter(Project.id.in_(allowed))
            site_q = site_q.filter(Site.project_id.in_(allowed))
            sample_q = sample_q.filter(SamplingEvent.project_id.in_(allowed))
            meas_q = meas_q.filter(SamplingEvent.project_id.in_(allowed))
        return DashboardSummary(
            projects=proj_q.count(),
            sites=site_q.count(),
            samples=sample_q.count(),
            variables=db.query(Variable).count(),
            measurements=meas_q.count(),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@public_router.get("/public-summary", response_model=PublicSummary)
async def get_public_summary():
    """Counts for the landing page's stats teaser — anonymous, PUBLIC-access only.

    `accessible_project_ids(db, None)` already resolves to PUBLIC-only (and
    past-embargo) projects for a null user, so this only ever surfaces data
    that's already meant to be open. "Datasets" counts projects that have a
    FAIR metadata record (same definition as the dataset catalogue).
    """
    db = SessionLocal()
    try:
        allowed = accessible_project_ids(db, None) or set()
        dataset_q = db.query(ProjectMetadata).join(Project, ProjectMetadata.project_id == Project.id).filter(Project.id.in_(allowed))
        site_q = db.query(Site).filter(Site.project_id.in_(allowed))
        sample_q = (
            db.query(Sample)
            .join(SamplingEvent, Sample.event_id == SamplingEvent.id)
            .filter(SamplingEvent.project_id.in_(allowed))
        )
        country_q = (
            db.query(Site.country)
            .filter(Site.project_id.in_(allowed), Site.country.isnot(None))
            .distinct()
        )
        return PublicSummary(
            datasets=dataset_q.count(),
            sites=site_q.count(),
            samples=sample_q.count(),
            countries=country_q.count(),
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
    country: Optional[str] = Query(None),
    depth_from: Optional[float] = Query(None),
    depth_to: Optional[float] = Query(None),
    skip: int = Query(0, ge=0),
    # Up to 50k so the explorer can pull its dataset in a couple of requests
    # instead of hundreds of 200-row pages (gzip keeps the payload ~1 MB).
    limit: int = Query(25, ge=1, le=50000),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """Measurement records, one row per measurement, with every dashboard filter applied.
    Paginated; the explorer pulls large pages from here.
    """
    db = SessionLocal()
    try:
        query = _base_records_query(db)
        query = _apply_record_filters(
            query, project_id, site_id, sample_type_id, matrix, variable_id, date_from, date_to, search,
            country=country, depth_from=depth_from, depth_to=depth_to,
        )
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        if allowed is not None:
            query = query.filter(Project.id.in_(allowed))

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

            latitude, longitude = _clean_coords(row.latitude, row.longitude)

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
                    latitude=latitude,
                    longitude=longitude,
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
                    depth_from=float(row.depth_from) if row.depth_from is not None else None,
                    depth_to=float(row.depth_to) if row.depth_to is not None else None,
                    depth_cm=_midpoint(row.depth_from, row.depth_to),
                    laboratory=row.laboratory,
                    status=status,
                )
            )

        return DashboardRecordsResponse(items=items, total=total, skip=skip, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/wide")
async def get_wide_dataset(
    variable_ids: List[int] = Query(default=[]),
    all_variables: bool = Query(False),
    matrix: Optional[str] = Query(None),
    project_id: Optional[int] = Query(None),
    site_id: Optional[int] = Query(None),
    sample_type_id: Optional[int] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    # Matches the explorer's own MAX_ROWS (GraphicWalkerView.tsx) — Graphic
    # Walker's own docs call 10K-100K rows "Good", so the frontend always
    # requests up to 100000; this cap must not be lower or every load 422s.
    limit: int = Query(5000, ge=1, le=100000),
    lang: str = Query("en", description="Dimension label language: 'en' (default) or 'es'."),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """Pivoted ("wide") dataset for the Graphic Walker explorer.

    One row per sample, one numeric column per variable (mean value), plus site
    coordinates. Enables scatter/correlation between variables and maps. Pass
    ``all_variables=true`` to expose every variable as a draggable column, or a
    list of ``variable_ids`` to restrict the columns.
    """
    if not variable_ids and not all_variables:
        return {"columns": [], "rows": [], "total_samples": 0}

    db = SessionLocal()
    try:
        var_query = db.query(Variable)
        if not all_variables:
            var_query = var_query.filter(Variable.id.in_(variable_ids))
        if matrix is not None:
            var_query = var_query.filter(Variable.matrix == matrix)
        variables = var_query.all()
        var_by_id = {v.id: v for v in variables}

        query = (
            db.query(
                Sample.id.label("sample_id"),
                Sample.sample_code.label("sample_code"),
                Project.name.label("project_name"),
                Site.site_code.label("site_code"),
                Site.country.label("country"),
                func.ST_Y(func.ST_Centroid(Site.geom)).label("latitude"),
                func.ST_X(func.ST_Centroid(Site.geom)).label("longitude"),
                SampleType.name.label("sample_type"),
                SamplingEvent.event_date.label("event_date"),
                _depth_scalar(db, DEPTH_FROM_CODES).label("depth_from"),
                _depth_scalar(db, DEPTH_TO_CODES).label("depth_to"),
                Measurement.variable_id.label("variable_id"),
                func.avg(Measurement.value_numeric).label("val"),
            )
            .select_from(Measurement)
            .join(Sample, Measurement.sample_id == Sample.id)
            .join(SampleType, Sample.sample_type_id == SampleType.id)
            .join(SamplingEvent, Sample.event_id == SamplingEvent.id)
            .join(Project, SamplingEvent.project_id == Project.id)
            .outerjoin(Site, Sample.site_id == Site.id)
            .filter(Measurement.value_numeric.isnot(None))
            .group_by(
                Sample.id, Sample.sample_code, Project.name, Site.site_code, Site.country,
                Site.geom, SampleType.name, SamplingEvent.event_date, Measurement.variable_id,
            )
            .order_by(Sample.id)
        )
        if not all_variables:
            query = query.filter(Measurement.variable_id.in_(variable_ids))
        if matrix is not None:
            query = query.join(Variable, Measurement.variable_id == Variable.id).filter(Variable.matrix == matrix)
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        if allowed is not None:
            query = query.filter(Project.id.in_(allowed))
        if project_id is not None:
            query = query.filter(Project.id == project_id)
        if site_id is not None:
            query = query.filter(Site.id == site_id)
        if sample_type_id is not None:
            query = query.filter(SampleType.id == sample_type_id)
        if date_from is not None:
            query = query.filter(SamplingEvent.event_date >= date_from)
        if date_to is not None:
            query = query.filter(SamplingEvent.event_date <= date_to)

        rows_by_sample: dict = {}
        order: list = []
        encountered: set = set()
        for r in query.all():
            row = rows_by_sample.get(r.sample_id)
            if row is None:
                # Rows arrive ordered by sample id, so once the cap is reached the
                # remaining rows all belong to new (excluded) samples.
                if len(order) >= limit:
                    break
                latitude, longitude = _clean_coords(r.latitude, r.longitude)
                row = {
                    "sample_code": r.sample_code,
                    "project_name": r.project_name,
                    "site_code": r.site_code,
                    "country": r.country,
                    "latitude": latitude,
                    "longitude": longitude,
                    "sample_type": r.sample_type,
                    "event_date": r.event_date.isoformat() if r.event_date else None,
                    "depth_cm": _midpoint(r.depth_from, r.depth_to),
                }
                rows_by_sample[r.sample_id] = row
                order.append(r.sample_id)
            var = var_by_id.get(r.variable_id)
            if var is not None and r.val is not None:
                row[var.variable_code] = float(r.val)
                encountered.add(r.variable_id)

        rows = [rows_by_sample[k] for k in order]

        dim_specs = [
            ("project_name", "nominal"),
            ("site_code", "nominal"),
            ("sample_code", "nominal"),
            ("sample_type", "nominal"),
            ("country", "nominal"),
            ("event_date", "temporal"),
            ("depth_cm", "quantitative"),
            ("latitude", "quantitative"),
            ("longitude", "quantitative"),
        ]
        columns = [
            {"key": key, "label": _wide_label(key, lang), "role": "dimension", "semantic": semantic}
            for key, semantic in dim_specs
        ]
        # With all_variables we only expose the variables actually present (sorted by
        # name); otherwise keep the requested order.
        if all_variables:
            measure_vids = sorted(encountered, key=lambda vid: (var_by_id[vid].name or "").lower())
        else:
            measure_vids = variable_ids
        for vid in measure_vids:
            v = var_by_id.get(vid)
            if v is None:
                continue
            label = v.name + (f" — {v.matrix}" if v.matrix else "")
            columns.append({"key": v.variable_code, "label": label, "role": "measure", "semantic": "quantitative"})

        return {"columns": columns, "rows": rows, "total_samples": len(rows)}
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
    country: Optional[str] = Query(None),
    depth_from: Optional[float] = Query(None),
    depth_to: Optional[float] = Query(None),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """The same records as `/dashboard/records`, streamed as a downloadable file instead of
    JSON.
    """
    db = SessionLocal()
    try:
        query = _base_records_query(db)
        query = _apply_record_filters(
            query, project_id, site_id, sample_type_id, matrix, variable_id, date_from, date_to, search,
            country=country, depth_from=depth_from, depth_to=depth_to,
        )
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        if allowed is not None:
            query = query.filter(Project.id.in_(allowed))
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
