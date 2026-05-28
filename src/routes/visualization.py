from csv import DictWriter
from datetime import date, datetime
from io import StringIO
from math import sqrt
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import DateTime, cast, func
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
from schemas.visualization import (
    BySiteResponse,
    CorrelationPoint,
    CorrelationResponse,
    SiteSeriesPoint,
    TimeSeriesPoint,
    TimeSeriesResponse,
)

router = APIRouter(prefix="/visualization", tags=["Visualization"])

_ALLOWED_PERIODS = {"day", "week", "month", "year"}
_ALLOWED_AGGREGATIONS = {"avg", "min", "max", "sum", "count"}


def _normalize_period(period: str) -> str:
    period = period.lower()
    if period not in _ALLOWED_PERIODS:
        raise HTTPException(status_code=400, detail=f"Unsupported period '{period}'")
    return period


def _normalize_aggregation(aggregation: str) -> str:
    aggregation = aggregation.lower()
    if aggregation not in _ALLOWED_AGGREGATIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported aggregation '{aggregation}'")
    return aggregation


def _period_label(period_value: datetime, period: str) -> str:
    if period == "day":
        return period_value.date().isoformat()
    if period == "week":
        year, week, _ = period_value.isocalendar()
        return f"{year}-W{week:02d}"
    if period == "month":
        return period_value.strftime("%Y-%m")
    return period_value.strftime("%Y")


def _aggregation_expression(aggregation: str):
    if aggregation == "avg":
        return func.avg(Measurement.value_numeric)
    if aggregation == "min":
        return func.min(Measurement.value_numeric)
    if aggregation == "max":
        return func.max(Measurement.value_numeric)
    if aggregation == "sum":
        return func.sum(Measurement.value_numeric)
    return func.count(Measurement.value_numeric)


def _base_filtered_query(
    db,
    project_id: Optional[int] = None,
    site_id: Optional[int] = None,
    matrix: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
):
    query = (
        db.query(Measurement, Sample, SamplingEvent, Project, Site, Variable, SampleType, LabBatch, Lab, Unit)
        .join(Sample, Measurement.sample_id == Sample.id)
        .join(SamplingEvent, Sample.event_id == SamplingEvent.id)
        .join(Project, SamplingEvent.project_id == Project.id)
        .join(SampleType, Sample.sample_type_id == SampleType.id)
        .join(Variable, Measurement.variable_id == Variable.id)
        .outerjoin(Site, Sample.site_id == Site.id)
        .outerjoin(LabBatch, Measurement.lab_batch_id == LabBatch.id)
        .outerjoin(Lab, LabBatch.lab_id == Lab.id)
        .outerjoin(Unit, Measurement.unit_id == Unit.id)
    )

    query = query.filter(SamplingEvent.event_date.isnot(None))

    if project_id is not None:
        query = query.filter(Project.id == project_id)
    if site_id is not None:
        query = query.filter(Site.id == site_id)
    if matrix is not None:
        query = query.filter(Variable.matrix == matrix)
    if date_from is not None:
        query = query.filter(SamplingEvent.event_date >= date_from)
    if date_to is not None:
        query = query.filter(SamplingEvent.event_date <= date_to)

    return query


def _build_timeseries(
    db,
    project_id: int,
    variable_id: Optional[int] = None,
    variable_ids: Optional[list[int]] = None,
    site_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    aggregation: str = "avg",
    period: str = "month",
    matrix: Optional[str] = None,
) -> TimeSeriesResponse:
    period = _normalize_period(period)
    aggregation = _normalize_aggregation(aggregation)
    selected_variable_ids = list(variable_ids or [])
    if variable_id is not None:
        selected_variable_ids.append(variable_id)
    if not selected_variable_ids:
        raise HTTPException(status_code=400, detail="Provide variable_id or variable_ids")

    period_expr = func.date_trunc(period, cast(SamplingEvent.event_date, DateTime)).label("period_value")
    aggregate_expr = _aggregation_expression(aggregation).label("aggregated_value")

    rows = (
        _base_filtered_query(db, project_id=project_id, site_id=site_id, matrix=matrix, date_from=date_from, date_to=date_to)
        .filter(Measurement.variable_id.in_(selected_variable_ids))
        .filter(Measurement.value_numeric.isnot(None))
        .with_entities(
            period_expr,
            Variable.id.label("variable_id"),
            Variable.name.label("variable_name"),
            aggregate_expr,
        )
        .group_by(period_expr, Variable.id, Variable.name)
        .order_by(period_expr.asc(), Variable.name.asc())
        .all()
    )

    labels = []
    labels_index = {}
    series_map = {}

    for row in rows:
        label = _period_label(row.period_value, period)
        if label not in labels_index:
            labels_index[label] = len(labels)
            labels.append(label)
        series_key = (row.variable_id, row.variable_name)
        if series_key not in series_map:
            series_map[series_key] = [None] * len(labels)
        for values in series_map.values():
            while len(values) < len(labels):
                values.append(None)
        series_map[series_key][labels_index[label]] = float(row.aggregated_value) if row.aggregated_value is not None else None

    series = [
        TimeSeriesPoint(variable_id=variable_id, variable_name=variable_name, values=values)
        for (variable_id, variable_name), values in series_map.items()
    ]
    return TimeSeriesResponse(labels=labels, series=series)


def _build_by_site(
    db,
    project_id: int,
    variable_id: Optional[int] = None,
    secondary_variable_id: Optional[int] = None,
    variable_ids: Optional[list[int]] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    aggregation: str = "avg",
    matrix: Optional[str] = None,
) -> BySiteResponse:
    aggregation = _normalize_aggregation(aggregation)
    selected_variable_ids = list(variable_ids or [])
    if variable_id is not None:
        selected_variable_ids.append(variable_id)
    if secondary_variable_id is not None:
        selected_variable_ids.append(secondary_variable_id)
    if not selected_variable_ids:
        raise HTTPException(status_code=400, detail="Provide variable_id, secondary_variable_id, or variable_ids")

    aggregate_expr = _aggregation_expression(aggregation).label("aggregated_value")
    rows = (
        _base_filtered_query(db, project_id=project_id, matrix=matrix, date_from=date_from, date_to=date_to)
        .filter(Measurement.variable_id.in_(selected_variable_ids))
        .filter(Measurement.value_numeric.isnot(None))
        .with_entities(
            Site.id.label("site_id"),
            Site.site_code.label("site_code"),
            Variable.id.label("variable_id"),
            Variable.name.label("variable_name"),
            aggregate_expr,
        )
        .group_by(Site.id, Site.site_code, Variable.id, Variable.name)
        .order_by(Site.site_code.asc(), Variable.name.asc())
        .all()
    )

    categories = []
    categories_index = {}
    series_map = {}

    for row in rows:
        site_label = row.site_code or f"Site {row.site_id}"
        if site_label not in categories_index:
            categories_index[site_label] = len(categories)
            categories.append(site_label)
        series_key = row.variable_name
        if series_key not in series_map:
            series_map[series_key] = [None] * len(categories)
        for values in series_map.values():
            while len(values) < len(categories):
                values.append(None)
        series_map[series_key][categories_index[site_label]] = float(row.aggregated_value) if row.aggregated_value is not None else None

    series = [SiteSeriesPoint(name=name, values=values) for name, values in series_map.items()]
    return BySiteResponse(categories=categories, series=series)


def _build_correlation(
    db,
    project_id: int,
    variable_x_id: int,
    variable_y_id: int,
    site_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    matrix: Optional[str] = None,
) -> CorrelationResponse:
    x_query = _base_filtered_query(db, project_id=project_id, site_id=site_id, matrix=matrix, date_from=date_from, date_to=date_to)
    y_query = _base_filtered_query(db, project_id=project_id, site_id=site_id, matrix=matrix, date_from=date_from, date_to=date_to)

    x_subquery = (
        x_query.filter(Measurement.variable_id == variable_x_id)
        .filter(Measurement.value_numeric.isnot(None))
        .with_entities(
            Sample.id.label("sample_id"),
            func.avg(Measurement.value_numeric).label("x"),
        )
        .group_by(Sample.id)
        .subquery()
    )
    y_subquery = (
        y_query.filter(Measurement.variable_id == variable_y_id)
        .filter(Measurement.value_numeric.isnot(None))
        .with_entities(
            Sample.id.label("sample_id"),
            func.avg(Measurement.value_numeric).label("y"),
        )
        .group_by(Sample.id)
        .subquery()
    )

    rows = db.query(x_subquery.c.x, y_subquery.c.y).join(y_subquery, x_subquery.c.sample_id == y_subquery.c.sample_id).all()
    points = [CorrelationPoint(x=float(row.x), y=float(row.y)) for row in rows]

    correlation = None
    if len(points) >= 2:
        xs = [point.x for point in points]
        ys = [point.y for point in points]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        denom_x = sqrt(sum((x - mean_x) ** 2 for x in xs))
        denom_y = sqrt(sum((y - mean_y) ** 2 for y in ys))
        if denom_x and denom_y:
            correlation = numerator / (denom_x * denom_y)

    return CorrelationResponse(points=points, correlation=correlation)


@router.get("/timeseries", response_model=TimeSeriesResponse)
async def get_timeseries(
    project_id: int = Query(..., ge=1),
    variable_id: Optional[int] = Query(None),
    variable_ids: Optional[list[int]] = Query(None),
    site_id: Optional[int] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    aggregation: str = Query("avg"),
    period: str = Query("month"),
    matrix: Optional[str] = Query(None),
):
    db = SessionLocal()
    try:
        return _build_timeseries(
            db,
            project_id=project_id,
            variable_id=variable_id,
            variable_ids=variable_ids,
            site_id=site_id,
            date_from=date_from,
            date_to=date_to,
            aggregation=aggregation,
            period=period,
            matrix=matrix,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/by-site", response_model=BySiteResponse)
async def get_by_site(
    project_id: int = Query(..., ge=1),
    variable_id: Optional[int] = Query(None),
    secondary_variable_id: Optional[int] = Query(None),
    variable_ids: Optional[list[int]] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    aggregation: str = Query("avg"),
    matrix: Optional[str] = Query(None),
):
    db = SessionLocal()
    try:
        return _build_by_site(
            db,
            project_id=project_id,
            variable_id=variable_id,
            secondary_variable_id=secondary_variable_id,
            variable_ids=variable_ids,
            date_from=date_from,
            date_to=date_to,
            aggregation=aggregation,
            matrix=matrix,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/correlation", response_model=CorrelationResponse)
async def get_correlation(
    project_id: int = Query(..., ge=1),
    variable_x_id: int = Query(..., ge=1),
    variable_y_id: int = Query(..., ge=1),
    site_id: Optional[int] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    matrix: Optional[str] = Query(None),
):
    db = SessionLocal()
    try:
        return _build_correlation(
            db,
            project_id=project_id,
            variable_x_id=variable_x_id,
            variable_y_id=variable_y_id,
            site_id=site_id,
            date_from=date_from,
            date_to=date_to,
            matrix=matrix,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/export")
async def export_visualization_data(
    chart_type: str = Query(..., pattern="^(timeseries|by-site|correlation)$"),
    project_id: int = Query(..., ge=1),
    variable_id: Optional[int] = Query(None),
    variable_ids: Optional[list[int]] = Query(None),
    secondary_variable_id: Optional[int] = Query(None),
    variable_x_id: Optional[int] = Query(None),
    variable_y_id: Optional[int] = Query(None),
    site_id: Optional[int] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    aggregation: str = Query("avg"),
    period: str = Query("month"),
    matrix: Optional[str] = Query(None),
):
    db = SessionLocal()
    try:
        buffer = StringIO()
        writer = DictWriter(buffer, fieldnames=[])

        if chart_type == "timeseries":
            response = _build_timeseries(
                db,
                project_id=project_id,
                variable_id=variable_id,
                variable_ids=variable_ids,
                site_id=site_id,
                date_from=date_from,
                date_to=date_to,
                aggregation=aggregation,
                period=period,
                matrix=matrix,
            )
            fieldnames = ["label"] + [f"variable_{series.variable_id}" for series in response.series]
            writer = DictWriter(buffer, fieldnames=fieldnames)
            writer.writeheader()
            for index, label in enumerate(response.labels):
                row = {"label": label}
                for series in response.series:
                    row[f"variable_{series.variable_id}"] = series.values[index] if index < len(series.values) else None
                writer.writerow(row)

        elif chart_type == "by-site":
            response = _build_by_site(
                db,
                project_id=project_id,
                variable_id=variable_id,
                secondary_variable_id=secondary_variable_id,
                variable_ids=variable_ids,
                date_from=date_from,
                date_to=date_to,
                aggregation=aggregation,
                matrix=matrix,
            )
            fieldnames = ["site"] + [series.name for series in response.series]
            writer = DictWriter(buffer, fieldnames=fieldnames)
            writer.writeheader()
            for index, category in enumerate(response.categories):
                row = {"site": category}
                for series in response.series:
                    row[series.name] = series.values[index] if index < len(series.values) else None
                writer.writerow(row)

        else:
            if variable_x_id is None or variable_y_id is None:
                raise HTTPException(status_code=400, detail="variable_x_id and variable_y_id are required for correlation export")
            response = _build_correlation(
                db,
                project_id=project_id,
                variable_x_id=variable_x_id,
                variable_y_id=variable_y_id,
                site_id=site_id,
                date_from=date_from,
                date_to=date_to,
                matrix=matrix,
            )
            writer = DictWriter(buffer, fieldnames=["x", "y"])
            writer.writeheader()
            for point in response.points:
                writer.writerow(point.model_dump())

        content = buffer.getvalue()
        filename = f"{chart_type}_export.csv"
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()
