from csv import DictWriter
from datetime import date, datetime
from io import StringIO
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, or_
from sqlalchemy.orm import aliased

from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import (
    Measurement,
    Project,
    Sample,
    Site,
    SiteAttribute,
    SiteAttributeValue,
    Unit,
    Variable,
    SamplingEvent,
)
from schemas.map import (
    MapSummary,
    SiteAttributeItem,
    SiteMapDetail,
    SiteMapItem,
    SiteMeasurementSummary,
)

router = APIRouter(prefix="/map", tags=["Map"])


def _parse_bbox(bbox: Optional[str]):
    if not bbox:
        return None
    parts = [part.strip() for part in bbox.split(",")]
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox must be west,south,east,north")
    try:
        west, south, east, north = (float(part) for part in parts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bbox must contain numeric values") from exc
    return west, south, east, north


def _site_base_query(db):
    measurement_count = func.count(Measurement.id).label("measurement_count")
    last_event_date = func.max(SamplingEvent.event_date).label("last_event_date")
    last_updated = func.greatest(
        func.coalesce(func.max(Site.updated_at), func.now()),
        func.coalesce(func.max(Measurement.updated_at), func.now()),
    ).label("last_updated")
    latitude = func.ST_Y(Site.geom).label("latitude")
    longitude = func.ST_X(Site.geom).label("longitude")

    return (
        db.query(
            Site.id.label("site_id"),
            Site.site_code.label("site_code"),
            Site.project_id.label("project_id"),
            Project.name.label("project_name"),
            Site.country.label("country"),
            Site.admin_1.label("admin_1"),
            Site.admin_2.label("admin_2"),
            Site.admin_3.label("admin_3"),
            latitude,
            longitude,
            measurement_count,
            last_event_date,
            last_updated,
        )
        .select_from(Site)
        .join(Project, Site.project_id == Project.id)
        .outerjoin(Sample, Sample.site_id == Site.id)
        .outerjoin(Measurement, Measurement.sample_id == Sample.id)
        .outerjoin(SamplingEvent, Sample.event_id == SamplingEvent.id)
    )


def _apply_site_filters(
    query,
    project_id: Optional[int] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    bbox: Optional[str] = None,
    north: Optional[float] = None,
    south: Optional[float] = None,
    east: Optional[float] = None,
    west: Optional[float] = None,
):
    if project_id is not None:
        query = query.filter(Site.project_id == project_id)

    bbox_values = _parse_bbox(bbox)
    if bbox_values:
        west, south, east, north = bbox_values
    if north is not None and south is not None and east is not None and west is not None:
        query = query.filter(
            func.ST_Y(Site.geom).between(south, north),
            func.ST_X(Site.geom).between(west, east),
        )

    if search:
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                Site.site_code.ilike(pattern),
                Site.country.ilike(pattern),
                Site.admin_1.ilike(pattern),
                Site.admin_2.ilike(pattern),
                Site.admin_3.ilike(pattern),
                Project.name.ilike(pattern),
            )
        )

    if status is not None:
        normalized = status.lower()
        if normalized not in {"active", "inactive"}:
            raise HTTPException(status_code=400, detail="status must be active or inactive")
        if normalized == "active":
            query = query.having(func.count(Measurement.id) > 0)
        else:
            query = query.having(func.count(Measurement.id) == 0)

    return query.group_by(
        Site.id,
        Site.site_code,
        Site.project_id,
        Project.name,
        Site.country,
        Site.admin_1,
        Site.admin_2,
        Site.admin_3,
        Site.geom,
    )


def _latest_site_attributes(db, site_id: int):
    attribute_rows = (
        db.query(
            SiteAttribute.site_attribute_code.label("attribute_code"),
            SiteAttribute.name.label("attribute_name"),
            SiteAttribute.value_type.label("value_type"),
            SiteAttributeValue.value_numeric.label("value_numeric"),
            SiteAttributeValue.value_text.label("value_text"),
            SiteAttributeValue.value_boolean.label("value_boolean"),
            SiteAttributeValue.value_date.label("value_date"),
            SiteAttributeValue.effective_date.label("effective_date"),
            SiteAttributeValue.source_name.label("source_name"),
            Unit.symbol.label("unit_symbol"),
            SiteAttributeValue.created_at.label("created_at"),
            SiteAttributeValue.id.label("value_id"),
        )
        .join(SiteAttributeValue, SiteAttributeValue.site_attribute_id == SiteAttribute.id)
        .outerjoin(Unit, SiteAttributeValue.unit_id == Unit.id)
        .filter(SiteAttributeValue.site_id == site_id)
        .order_by(SiteAttribute.site_attribute_code.asc(), SiteAttributeValue.effective_date.desc().nullslast(), SiteAttributeValue.created_at.desc(), SiteAttributeValue.id.desc())
        .all()
    )

    latest = {}
    for row in attribute_rows:
        if row.attribute_code in latest:
            continue
        value = row.value_numeric
        if value is None:
            value = row.value_text
        if value is None:
            value = row.value_boolean
        if value is None:
            value = row.value_date.isoformat() if row.value_date else None
        latest[row.attribute_code] = SiteAttributeItem(
            attribute_code=row.attribute_code,
            attribute_name=row.attribute_name,
            value=value,
            value_type=row.value_type,
            unit=row.unit_symbol,
            effective_date=row.effective_date,
            source_name=row.source_name,
        )
    return list(latest.values())


def _recent_site_variables(db, site_id: int, limit: int = 5):
    rows = (
        db.query(
            Variable.id.label("variable_id"),
            Variable.variable_code.label("variable_code"),
            Variable.name.label("variable_name"),
            Measurement.value_numeric.label("value_numeric"),
            Measurement.value_text.label("value_text"),
            Measurement.value_boolean.label("value_boolean"),
            Measurement.value_date.label("value_date"),
            Unit.symbol.label("unit_symbol"),
            SamplingEvent.event_date.label("event_date"),
            Measurement.updated_at.label("updated_at"),
        )
        .join(Sample, Measurement.sample_id == Sample.id)
        .join(SamplingEvent, Sample.event_id == SamplingEvent.id)
        .join(Variable, Measurement.variable_id == Variable.id)
        .outerjoin(Unit, Measurement.unit_id == Unit.id)
        .filter(Sample.site_id == site_id)
        .order_by(Measurement.updated_at.desc())
        .limit(limit)
        .all()
    )

    summaries = []
    seen = set()
    for row in rows:
        if row.variable_id in seen:
            continue
        seen.add(row.variable_id)
        value = row.value_numeric
        if value is None:
            value = row.value_text
        if value is None:
            value = row.value_boolean
        if value is None:
            value = row.value_date.isoformat() if row.value_date else None
        summaries.append(
            SiteMeasurementSummary(
                variable_id=row.variable_id,
                variable_name=row.variable_name,
                variable_code=row.variable_code,
                value=value,
                unit=row.unit_symbol,
                event_date=row.event_date,
                updated_at=row.updated_at,
            )
        )
    return summaries


@router.get("/summary", response_model=MapSummary)
async def get_map_summary(
    project_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    bbox: Optional[str] = Query(None),
    north: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    west: Optional[float] = Query(None),
):
    db = SessionLocal()
    try:
        query = _site_base_query(db)
        query = _apply_site_filters(query, project_id=project_id, search=search, bbox=bbox, north=north, south=south, east=east, west=west)
        rows = query.all()
        total_sites = len(rows)
        active_sites = sum(1 for row in rows if row.measurement_count and row.measurement_count > 0)
        inactive_sites = total_sites - active_sites

        dates = (
            db.query(func.min(SamplingEvent.event_date), func.max(SamplingEvent.event_date))
            .join(Sample, Sample.event_id == SamplingEvent.id)
            .join(Site, Sample.site_id == Site.id)
        )
        if project_id is not None:
            dates = dates.filter(SamplingEvent.project_id == project_id)
        if search:
            pattern = f"%{search}%"
            dates = dates.filter(
                or_(
                    Site.site_code.ilike(pattern),
                    Site.country.ilike(pattern),
                    Site.admin_1.ilike(pattern),
                    Site.admin_2.ilike(pattern),
                    Site.admin_3.ilike(pattern),
                    SamplingEvent.activity.ilike(pattern),
                )
            )
        date_from, date_to = dates.one()

        return MapSummary(
            total_sites=total_sites,
            active_sites=active_sites,
            inactive_sites=inactive_sites,
            selected_sites=0,
            date_from=date_from,
            date_to=date_to,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/sites", response_model=list[SiteMapItem])
async def get_map_sites(
    project_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    bbox: Optional[str] = Query(None),
    north: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    west: Optional[float] = Query(None),
):
    db = SessionLocal()
    try:
        query = _site_base_query(db)
        query = _apply_site_filters(query, project_id=project_id, status=status, search=search, bbox=bbox, north=north, south=south, east=east, west=west)
        rows = query.order_by(Site.site_code.asc()).all()

        results = []
        for row in rows:
            is_active = bool(row.measurement_count and row.measurement_count > 0)
            results.append(
                SiteMapItem(
                    id=row.site_id,
                    site_code=row.site_code,
                    project_id=row.project_id,
                    project_name=row.project_name,
                    latitude=float(row.latitude) if row.latitude is not None else None,
                    longitude=float(row.longitude) if row.longitude is not None else None,
                    status="active" if is_active else "inactive",
                    measurement_count=int(row.measurement_count or 0),
                    last_event_date=row.last_event_date,
                    last_updated=row.last_updated,
                    is_selected=False,
                    is_active=is_active,
                )
            )
        return results
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/sites/{site_id}", response_model=SiteMapDetail)
async def get_map_site_detail(site_id: int):
    db = SessionLocal()
    try:
        query = _site_base_query(db).filter(Site.id == site_id)
        row = query.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Site not found")

        attributes = _latest_site_attributes(db, site_id)
        recent_variables = _recent_site_variables(db, site_id)

        is_active = bool(row.measurement_count and row.measurement_count > 0)
        return SiteMapDetail(
            id=row.site_id,
            site_code=row.site_code,
            project_id=row.project_id,
            project_name=row.project_name,
            country=row.country,
            admin_1=row.admin_1,
            admin_2=row.admin_2,
            admin_3=row.admin_3,
            latitude=float(row.latitude) if row.latitude is not None else None,
            longitude=float(row.longitude) if row.longitude is not None else None,
            status="active" if is_active else "inactive",
            measurement_count=int(row.measurement_count or 0),
            last_event_date=row.last_event_date,
            last_updated=row.last_updated,
            attributes=attributes,
            recent_variables=recent_variables,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/sites/{site_id}/attributes", response_model=list[SiteAttributeItem])
async def get_map_site_attributes(site_id: int):
    db = SessionLocal()
    try:
        return _latest_site_attributes(db, site_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/export")
async def export_map_sites(
    project_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    bbox: Optional[str] = Query(None),
    north: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    west: Optional[float] = Query(None),
):
    db = SessionLocal()
    try:
        rows = _apply_site_filters(
            _site_base_query(db),
            project_id=project_id,
            status=status,
            search=search,
            bbox=bbox,
            north=north,
            south=south,
            east=east,
            west=west,
        ).order_by(Site.site_code.asc()).all()

        buffer = StringIO()
        writer = DictWriter(
            buffer,
            fieldnames=[
                "id",
                "site_code",
                "project_id",
                "project_name",
                "latitude",
                "longitude",
                "status",
                "measurement_count",
                "last_updated",
                "is_selected",
                "is_active",
            ],
        )
        writer.writeheader()
        for row in rows:
            is_active = bool(row.measurement_count and row.measurement_count > 0)
            writer.writerow(
                {
                    "id": row.site_id,
                    "site_code": row.site_code,
                    "project_id": row.project_id,
                    "project_name": row.project_name,
                    "latitude": float(row.latitude) if row.latitude is not None else None,
                    "longitude": float(row.longitude) if row.longitude is not None else None,
                    "status": "active" if is_active else "inactive",
                    "measurement_count": int(row.measurement_count or 0),
                    "last_event_date": row.last_event_date.isoformat() if row.last_event_date else None,
                    "last_updated": row.last_updated.isoformat() if row.last_updated else None,
                    "is_selected": False,
                    "is_active": is_active,
                }
            )

        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="map_sites.csv"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()
