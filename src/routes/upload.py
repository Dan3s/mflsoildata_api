"""Bulk upload endpoints (carga de datos).

Flow: template-columns -> template -> validate (dry-run) -> import (commit).
The validate/import pair shares the exact same ETL engine (``services.etl``).
"""

from typing import List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from dependencies.auth import require_editor

from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import Measurement, Sample, SampleAttribute, SampleType, SamplingEvent, Variable

from schemas.upload import (
    ImportRequest,
    ImportResponse,
    TemplateColumn,
    TemplateColumnGroup,
    TemplateColumnsResponse,
    ValidateResponse,
)
from services import etl

router = APIRouter(prefix="/upload", tags=["Upload"])


@router.get("/template-columns", response_model=TemplateColumnsResponse)
async def get_template_columns(project_id: Optional[int] = Query(None)):
    """Catalog of selectable columns for the upload template.

    Structural columns are static; sample attributes and variables come from the
    DB catalog (optionally narrowed to those already used by ``project_id``).
    """
    db = SessionLocal()
    try:
        structural = TemplateColumnGroup(
            key="estructura",
            label="Identificación (obligatorio)",
            description="Campos mínimos para ubicar la muestra en proyecto, sitio, evento y muestra.",
            columns=[
                TemplateColumn(
                    id=c.id, label=c.label, required=c.required, value_type=c.value_type,
                    unit=c.unit, db_target=c.db_target, example=c.example,
                )
                for c in etl.STRUCTURAL_COLUMNS
            ],
        )

        attributes = db.query(SampleAttribute).order_by(SampleAttribute.name.asc()).all()
        attr_group = TemplateColumnGroup(
            key="atributos_muestra",
            label="Atributos de muestra (opcional)",
            description="Se guardan como EAV en sample_attribute_values.",
            columns=[
                TemplateColumn(
                    id=f"attr:{a.sample_attribute_code}", label=a.name, required=False,
                    value_type=a.value_type, attribute_code=a.sample_attribute_code,
                    db_target="sample_attribute_values",
                )
                for a in attributes
            ],
        )

        var_query = db.query(Variable)
        if project_id is not None:
            var_query = (
                var_query.join(Measurement, Measurement.variable_id == Variable.id)
                .join(Sample, Measurement.sample_id == Sample.id)
                .join(SamplingEvent, Sample.event_id == SamplingEvent.id)
                .filter(SamplingEvent.project_id == project_id)
                .distinct()
            )
        variables = var_query.order_by(Variable.name.asc()).all()
        var_group = TemplateColumnGroup(
            key="variables",
            label="Variables / Mediciones (opcional)",
            description="Cada columna seleccionada genera measurements para esa variable.",
            columns=[
                TemplateColumn(
                    id=f"var:{v.id}", label=v.name, required=False, value_type=v.value_type,
                    matrix=v.matrix, variable_id=v.id, variable_code=v.variable_code,
                    db_target="measurements",
                )
                for v in variables
            ],
        )

        return TemplateColumnsResponse(groups=[structural, attr_group, var_group])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


def _resolve_requested_columns(db, columns: Optional[List[str]]):
    """Force required structural columns, then resolve the requested ids to specs."""
    requested = list(columns or [])
    ordered_ids: List[str] = []
    # Required structural columns always come first, in canonical order.
    for cid in etl.REQUIRED_STRUCTURAL_IDS:
        ordered_ids.append(cid)
    for cid in requested:
        cid = cid.strip()
        if cid and cid not in ordered_ids:
            ordered_ids.append(cid)
    specs, _ = etl.resolve_columns(db, ordered_ids)
    return specs


@router.get("/template")
async def get_template(
    columns: List[str] = Query(default=[]),
    format: str = Query("xlsx"),
    include_examples: bool = Query(False),
    project_id: Optional[int] = Query(None),
):
    """Generate and download the (xlsx/csv) template for the selected columns."""
    fmt = format.lower()
    if fmt not in {"xlsx", "csv"}:
        raise HTTPException(status_code=400, detail="format must be 'xlsx' or 'csv'")
    db = SessionLocal()
    try:
        specs = _resolve_requested_columns(db, columns)
        content, media_type, filename = etl.build_template(specs, fmt, include_examples)
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


@router.post("/validate", response_model=ValidateResponse, dependencies=[Depends(require_editor)])
async def validate_upload(
    file: UploadFile = File(...),
    columns: Optional[str] = Form(None),
):
    """Parse and validate a filled file without writing to the DB (dry-run).

    Returns an ``upload_id`` that can later be sent to ``/upload/import`` to
    commit exactly what was validated.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El archivo está vacío.")

    columns_hint = [c.strip() for c in columns.split(",")] if columns else None
    db = SessionLocal()
    try:
        try:
            parsed = etl.parse_file(content, file.filename, columns_hint)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        specs, column_issues = etl.resolve_columns(db, parsed.column_ids)
        known_sample_types = {st.sample_type_code for st in db.query(SampleType.sample_type_code).all()}
        issues, summary, valid_rows = etl.validate(parsed, specs, known_sample_types)
        issues = column_issues + issues

        error_count = sum(1 for i in issues if i["severity"] == "error")
        warning_count = sum(1 for i in issues if i["severity"] == "warning")

        upload_id = etl.new_upload_id()
        etl.upload_store.put(
            etl.StoredUpload(
                upload_id=upload_id,
                filename=file.filename or "carga.xlsx",
                column_ids=parsed.column_ids,
                rows=parsed.rows,
                issues=issues,
                summary=summary,
                error_count=error_count,
                warning_count=warning_count,
            )
        )

        return ValidateResponse(
            upload_id=upload_id,
            filename=file.filename or "carga.xlsx",
            total_rows=len(parsed.rows),
            valid_rows=valid_rows,
            error_count=error_count,
            warning_count=warning_count,
            summary=summary,
            issues=issues,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post("/import", response_model=ImportResponse, dependencies=[Depends(require_editor)])
async def import_upload(payload: ImportRequest = Body(...)):
    """Commit a previously validated upload (idempotent, single transaction)."""
    stored = etl.upload_store.get(payload.upload_id)
    if not stored:
        raise HTTPException(status_code=404, detail="upload_id no encontrado o expirado. Vuelva a validar el archivo.")

    if stored.error_count > 0:
        raise HTTPException(status_code=409, detail=f"El archivo tiene {stored.error_count} error(es). Corríjalos antes de importar.")
    if stored.warning_count > 0 and not payload.ignore_warnings:
        raise HTTPException(status_code=409, detail=f"El archivo tiene {stored.warning_count} advertencia(s). Reenvíe con ignore_warnings=true para continuar.")

    db = SessionLocal()
    try:
        result = etl.import_upload(db, stored)
        db.commit()
        etl.upload_store.pop(payload.upload_id)
        return ImportResponse(
            status="completed",
            inserted=result["inserted"],
            skipped_rows=result["skipped_rows"],
            message="Carga completada correctamente.",
        )
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error durante la importación: {exc}")
    finally:
        db.close()
