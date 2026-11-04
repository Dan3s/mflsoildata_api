from csv import DictWriter
from datetime import date, datetime
from io import StringIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, nullslast, or_
from sqlalchemy.orm import aliased

from dependencies.auth import CurrentUser, get_current_user, get_optional_user
from dependencies.access import accessible_project_ids, narrow, project_ids_by_institution
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import (
    Measurement,
    Project,
    ProjectMetadata,
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
    altitude = Site.altitude_m.label("altitude_m")

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
            altitude,
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
    search: Optional[str] = None,
    bbox: Optional[str] = None,
    north: Optional[float] = None,
    south: Optional[float] = None,
    east: Optional[float] = None,
    west: Optional[float] = None,
    country: Optional[str] = None,
    admin_1: Optional[str] = None,
    admin_2: Optional[str] = None,
    altitude_min: Optional[float] = None,
    altitude_max: Optional[float] = None,
):
    if project_id is not None:
        query = query.filter(Site.project_id == project_id)
    if country is not None:
        query = query.filter(Site.country == country)
    if admin_1 is not None:
        query = query.filter(Site.admin_1 == admin_1)
    if admin_2 is not None:
        query = query.filter(Site.admin_2 == admin_2)
    if altitude_min is not None:
        query = query.filter(Site.altitude_m >= altitude_min)
    if altitude_max is not None:
        query = query.filter(Site.altitude_m <= altitude_max)

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
        .outerjoin(SamplingEvent, Sample.event_id == SamplingEvent.id)
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
    country: Optional[str] = Query(None),
    admin_1: Optional[str] = Query(None),
    admin_2: Optional[str] = Query(None),
    altitude_min: Optional[float] = Query(None),
    altitude_max: Optional[float] = Query(None),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """Counts for the current map view, honouring the same filters and bounding box as the
    site list.
    """
    db = SessionLocal()
    try:
        query = _site_base_query(db)
        query = _apply_site_filters(
            query, project_id=project_id, search=search, bbox=bbox, north=north, south=south,
            east=east, west=west, country=country, admin_1=admin_1, admin_2=admin_2,
            altitude_min=altitude_min, altitude_max=altitude_max,
        )
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        if allowed is not None:
            query = query.filter(Site.project_id.in_(allowed))
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
    search: Optional[str] = Query(None),
    bbox: Optional[str] = Query(None),
    north: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    west: Optional[float] = Query(None),
    country: Optional[str] = Query(None),
    admin_1: Optional[str] = Query(None),
    admin_2: Optional[str] = Query(None),
    altitude_min: Optional[float] = Query(None),
    altitude_max: Optional[float] = Query(None),
    color_variable_id: Optional[int] = Query(None, description="Color markers by this variable's per-site mean."),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """Sites with coordinates for the map, filtered by project, text or a geographic
    bounding box.
    """
    db = SessionLocal()
    try:
        query = _site_base_query(db)
        query = _apply_site_filters(
            query, project_id=project_id, search=search, bbox=bbox, north=north, south=south,
            east=east, west=west, country=country, admin_1=admin_1, admin_2=admin_2,
            altitude_min=altitude_min, altitude_max=altitude_max,
        )
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        if allowed is not None:
            query = query.filter(Site.project_id.in_(allowed))
        rows = query.order_by(Site.site_code.asc()).all()

        # Per-site mean of the "color by" variable (one extra grouped query).
        color_by_site: dict = {}
        if color_variable_id is not None:
            for sid, avg_val in (
                db.query(Sample.site_id, func.avg(Measurement.value_numeric))
                .join(Measurement, Measurement.sample_id == Sample.id)
                .filter(Measurement.variable_id == color_variable_id, Measurement.value_numeric.isnot(None))
                .group_by(Sample.site_id)
                .all()
            ):
                color_by_site[sid] = float(avg_val) if avg_val is not None else None

        results = []
        for row in rows:
            is_active = bool(row.measurement_count and row.measurement_count > 0)
            results.append(
                SiteMapItem(
                    id=row.site_id,
                    site_code=row.site_code,
                    project_id=row.project_id,
                    project_name=row.project_name,
                    country=row.country,
                    admin_1=row.admin_1,
                    admin_2=row.admin_2,
                    altitude_m=float(row.altitude_m) if row.altitude_m is not None else None,
                    latitude=float(row.latitude) if row.latitude is not None else None,
                    longitude=float(row.longitude) if row.longitude is not None else None,
                    measurement_count=int(row.measurement_count or 0),
                    last_event_date=row.last_event_date,
                    last_updated=row.last_updated,
                    is_selected=False,
                    is_active=is_active,
                    color_value=color_by_site.get(row.site_id),
                )
            )
        return results
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/regions")
async def get_map_regions(
    country: Optional[str] = Query(None),
    admin_1: Optional[str] = Query(None),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """Distinct admin_1 (departments/regions) and admin_2 (municipalities) for the
    map filters — admin_2 narrowed to ``admin_1`` — within accessible projects."""
    db = SessionLocal()
    try:
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))

        def _base():
            q = db.query(Site)
            if allowed is not None:
                q = q.filter(Site.project_id.in_(allowed))
            if country:
                q = q.filter(Site.country == country)
            return q

        a1 = _base().with_entities(Site.admin_1).filter(Site.admin_1.isnot(None), Site.admin_1 != "").distinct()
        a2q = _base().with_entities(Site.admin_2).filter(Site.admin_2.isnot(None), Site.admin_2 != "")
        if admin_1:
            a2q = a2q.filter(Site.admin_1 == admin_1)
        return {
            "admin_1": sorted({r[0] for r in a1.all()}),
            "admin_2": sorted({r[0] for r in a2q.distinct().all()}),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/facets")
async def get_map_facets(
    project_id: Optional[int] = Query(None),
    country: Optional[str] = Query(None),
    admin_1: Optional[str] = Query(None),
    admin_2: Optional[str] = Query(None),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """What each map selector can offer, given what the others are set to (cascading filters).

    Every list is computed with all the *other* filters applied and its own one left out, so
    picking a country narrows the projects, picking a project narrows the countries, and
    neither list can ever shrink to the value already chosen. Counts are numbers of sites.
    """
    db = SessionLocal()
    try:
        access = accessible_project_ids(db, user)
        by_institution = project_ids_by_institution(db, institution)

        def sites(*, skip=(), with_institution=True):
            q = db.query(Site)
            allowed = narrow(access, by_institution) if with_institution else access
            if allowed is not None:
                q = q.filter(Site.project_id.in_(allowed))
            if project_id is not None and "project" not in skip:
                q = q.filter(Site.project_id == project_id)
            if country and "country" not in skip:
                q = q.filter(Site.country == country)
            if admin_1 and "admin_1" not in skip:
                q = q.filter(Site.admin_1 == admin_1)
            if admin_2 and "admin_2" not in skip:
                q = q.filter(Site.admin_2 == admin_2)
            return q

        projects = (
            sites(skip=("project",))
            .with_entities(Site.project_id, func.count(Site.id))
            .group_by(Site.project_id)
            .all()
        )
        countries = (
            sites(skip=("country", "admin_1", "admin_2"))
            .with_entities(Site.country, func.count(Site.id))
            .filter(Site.country.isnot(None), Site.country != "")
            .group_by(Site.country)
            .all()
        )
        scope_projects = {
            r[0] for r in sites(with_institution=False).with_entities(Site.project_id).distinct().all()
        }
        institutions = sorted(
            {
                r[0].strip()
                for r in db.query(ProjectMetadata.data_source)
                .filter(ProjectMetadata.project_id.in_(scope_projects), ProjectMetadata.data_source.isnot(None))
                .all()
                if r[0] and r[0].strip()
            }
        )
        return {
            "projects": [{"id": pid, "count": n} for pid, n in sorted(projects)],
            "countries": [{"value": c, "count": n} for c, n in sorted(countries)],
            "institutions": institutions,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/variables")
async def get_map_variables(
    project_id: Optional[int] = Query(None),
    country: Optional[str] = Query(None),
    admin_1: Optional[str] = Query(None),
    admin_2: Optional[str] = Query(None),
    altitude_min: Optional[float] = Query(None),
    altitude_max: Optional[float] = Query(None),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """Numeric variables actually measured within the current map scope, for the
    'color by variable' picker — so only relevant variables are offered."""
    db = SessionLocal()
    try:
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        q = (
            db.query(Variable.id, Variable.name, Variable.matrix)
            .join(Measurement, Measurement.variable_id == Variable.id)
            .join(Sample, Measurement.sample_id == Sample.id)
            .join(Site, Sample.site_id == Site.id)
            .filter(Measurement.value_numeric.isnot(None))
        )
        if allowed is not None:
            q = q.filter(Site.project_id.in_(allowed))
        if project_id is not None:
            q = q.filter(Site.project_id == project_id)
        if country is not None:
            q = q.filter(Site.country == country)
        if admin_1 is not None:
            q = q.filter(Site.admin_1 == admin_1)
        if admin_2 is not None:
            q = q.filter(Site.admin_2 == admin_2)
        if altitude_min is not None:
            q = q.filter(Site.altitude_m >= altitude_min)
        if altitude_max is not None:
            q = q.filter(Site.altitude_m <= altitude_max)

        labels: dict = {}
        for vid, name, matrix in q.distinct().all():
            labels[vid] = f"{name} — {matrix}" if matrix else name
        return [
            {"id": vid, "label": label}
            for vid, label in sorted(labels.items(), key=lambda kv: kv[1].lower())
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/sites/{site_id}", response_model=SiteMapDetail)
async def get_map_site_detail(
    site_id: int,
    recent_limit: int = Query(5, ge=1, le=50),
    recent_only: bool = Query(False),
    user: CurrentUser = Depends(get_current_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """One site with its recent measurements, for the map's detail panel."""
    db = SessionLocal()
    try:
        query = _apply_site_filters(_site_base_query(db), project_id=None).filter(Site.id == site_id)
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        if allowed is not None:
            query = query.filter(Site.project_id.in_(allowed))
        row = query.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Site not found")
        # fetch attributes and recent variables unless client requested only recent measurements
        attributes = []
        recent_variables = []
        if not recent_only:
            attributes = _latest_site_attributes(db, site_id)
            recent_variables = _recent_site_variables(db, site_id)

        # fetch recent measurements (only with event_date not null)
        recent_rows = (
            db.query(
                Measurement.id.label("id"),
                Measurement.measurement_code.label("measurement_code"),
                Variable.id.label("variable_id"),
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
            .outerjoin(SamplingEvent, Sample.event_id == SamplingEvent.id)
            .join(Variable, Measurement.variable_id == Variable.id)
            .outerjoin(Unit, Measurement.unit_id == Unit.id)
            .filter(Sample.site_id == site_id)
            # Include measurements even when the event has no date (they'd be
            # dropped otherwise); undated ones sort last.
            .order_by(nullslast(SamplingEvent.event_date.desc()), Measurement.id.desc())
            .limit(recent_limit)
            .all()
        )

        recent_measurements = []
        for r in recent_rows:
            value = r.value_numeric
            if value is None:
                value = r.value_text
            if value is None:
                value = r.value_boolean
            if value is None:
                value = r.value_date.isoformat() if r.value_date else None
            recent_measurements.append(
                {
                    "id": r.id,
                    "measurement_code": r.measurement_code,
                    "variable_id": r.variable_id,
                    "variable_name": r.variable_name,
                    "value": value,
                    "unit": r.unit_symbol,
                    "event_date": r.event_date,
                }
            )

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
            altitude_m=row.altitude_m,
            latitude=float(row.latitude) if row.latitude is not None else None,
            longitude=float(row.longitude) if row.longitude is not None else None,
            measurement_count=int(row.measurement_count or 0),
            last_event_date=row.last_event_date,
            last_updated=row.last_updated,
            attributes=attributes,
            recent_variables=recent_variables,
            recent_measurements=recent_measurements,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/sites/{site_id}/attributes", response_model=list[SiteAttributeItem])
async def get_map_site_attributes(site_id: int, user: CurrentUser = Depends(get_current_user)):
    """Latest value of each attribute recorded for a site (slope, land use, texture...)."""
    db = SessionLocal()
    try:
        site = db.query(Site).filter(Site.id == site_id).first()
        if not site:
            raise HTTPException(status_code=404, detail="Site not found")
        allowed = accessible_project_ids(db, user)
        if allowed is not None and site.project_id not in allowed:
            raise HTTPException(status_code=404, detail="Site not found")
        return _latest_site_attributes(db, site_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/export")
async def export_map_sites(
    project_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    bbox: Optional[str] = Query(None),
    north: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    west: Optional[float] = Query(None),
    country: Optional[str] = Query(None),
    admin_1: Optional[str] = Query(None),
    admin_2: Optional[str] = Query(None),
    altitude_min: Optional[float] = Query(None),
    altitude_max: Optional[float] = Query(None),
    user: Optional[CurrentUser] = Depends(get_optional_user),
    institution: Optional[str] = Query(None, description="Institution that owns the data (dataset data_source)"),
):
    """The sites currently shown on the map, as a downloadable file."""
    db = SessionLocal()
    try:
        query = _apply_site_filters(
            _site_base_query(db),
            project_id=project_id,
            search=search,
            bbox=bbox,
            north=north,
            south=south,
            east=east,
            west=west,
            country=country,
            admin_1=admin_1,
            admin_2=admin_2,
            altitude_min=altitude_min,
            altitude_max=altitude_max,
        )
        allowed = accessible_project_ids(db, user)
        # Same shape as the access filter, so both narrow the same project set.
        allowed = narrow(allowed, project_ids_by_institution(db, institution))
        if allowed is not None:
            query = query.filter(Site.project_id.in_(allowed))
        rows = query.order_by(Site.site_code.asc()).all()

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
