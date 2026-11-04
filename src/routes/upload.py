"""Bulk upload endpoints (carga de datos).

Flow: template-columns -> template -> validate (dry-run) -> import (commit).
The validate/import pair shares the exact same ETL engine (``services.etl``).
"""

import json
import os
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from fastapi.responses import Response

from dependencies.auth import CurrentUser, get_current_user, require_editor

from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import (
    DatasetSubmission,
    Measurement,
    Sample,
    SampleAttribute,
    SampleType,
    SamplingEvent,
    SubmissionRow,
    Unit,
    Variable,
)
from schemas.metadata import QC_FIELDS, ProjectMetadataUpsert
from services.email import send_email
from services import keycloak_admin

from schemas.upload import (
    ColumnMappingEntry,
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
async def get_template_columns(
    project_id: Optional[int] = Query(None),
    lang: str = Query(etl.LANG_DEFAULT, description="Label language: 'en' (default) or 'es'."),
):
    """Catalog of selectable columns for the upload template.

    Structural columns are static; sample attributes and variables come from the
    DB catalog (optionally narrowed to those already used by ``project_id``).
    Labels are English by default; pass ``lang=es`` for Spanish.
    """
    db = SessionLocal()
    try:
        structural = TemplateColumnGroup(
            key="estructura",
            label=etl._t(etl.GROUP_LABELS["estructura"], lang),
            description=etl._t(etl.GROUP_DESCRIPTIONS["estructura"], lang),
            columns=[
                TemplateColumn(
                    id=c.id, label=etl.structural_label(c.id, lang), required=c.required, value_type=c.value_type,
                    unit=c.unit, db_target=c.db_target, example=c.example,
                )
                for c in etl.STRUCTURAL_COLUMNS
            ],
        )

        attributes = db.query(SampleAttribute).order_by(SampleAttribute.name.asc()).all()
        attr_group = TemplateColumnGroup(
            key="atributos_muestra",
            label=etl._t(etl.GROUP_LABELS["atributos_muestra"], lang),
            description=etl._t(etl.GROUP_DESCRIPTIONS["atributos_muestra"], lang),
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

        # Group variables by matrix so the same analyte in different matrices
        # (e.g. "Zn Total (mg/kg)" en SOIL / LEAF / GRAIN) no se vea como duplicado.
        by_matrix: dict = {}
        for v in variables:
            by_matrix.setdefault(v.matrix or "", []).append(v)

        ordered_keys = [m for m in etl.MATRIX_ORDER if m in by_matrix]
        ordered_keys += [k for k in by_matrix if k and k not in etl.MATRIX_ORDER]
        if "" in by_matrix:
            ordered_keys.append("")

        var_groups = [
            TemplateColumnGroup(
                key=f"variables_{key.lower() or 'none'}",
                label=etl.matrix_group_label(key, lang),
                description=etl._t(etl.GROUP_DESCRIPTIONS["variables"], lang),
                columns=[
                    TemplateColumn(
                        id=f"var:{v.id}", label=v.name, required=False, value_type=v.value_type,
                        matrix=v.matrix, variable_id=v.id, variable_code=v.variable_code,
                        db_target="measurements",
                    )
                    for v in by_matrix[key]
                ],
            )
            for key in ordered_keys
        ]

        return TemplateColumnsResponse(groups=[structural, attr_group, *var_groups])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


def _resolve_requested_columns(db, columns: Optional[List[str]], lang: str = etl.LANG_DEFAULT):
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
    specs, _ = etl.resolve_columns(db, ordered_ids, lang)
    return specs


@router.get("/template")
async def get_template(
    columns: List[str] = Query(default=[]),
    format: str = Query("xlsx"),
    include_examples: bool = Query(False),
    project_id: Optional[int] = Query(None),
    lang: str = Query(etl.LANG_DEFAULT, description="Template language: 'en' (default) or 'es'."),
):
    """Generate and download the (xlsx/csv) template for the selected columns."""
    fmt = format.lower()
    if fmt not in {"xlsx", "csv"}:
        raise HTTPException(status_code=400, detail="format must be 'xlsx' or 'csv'")
    db = SessionLocal()
    try:
        specs = _resolve_requested_columns(db, columns, lang)
        units = [s for (s,) in db.query(Unit.symbol).order_by(Unit.symbol.asc()).all() if s]
        domains = [d for (d,) in db.query(Variable.domain).distinct().order_by(Variable.domain.asc()).all() if d]
        content, media_type, filename = etl.build_template(specs, fmt, include_examples, lang, units, domains)
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
    column_mapping: Optional[str] = Form(
        None, description="JSON-encoded list of ColumnMappingEntry, from the column-mapping UI."
    ),
    header_row: Optional[int] = Form(
        None, description="1-based row number (blanks included) that holds the file's header."
    ),
):
    """Parse and validate a filled file without writing to the DB (dry-run).

    Returns an ``upload_id`` that can later be sent to ``/upload/import`` to
    commit exactly what was validated.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The file is empty.")

    columns_hint = [c.strip() for c in columns.split(",")] if columns else None

    mapping: Optional[List[ColumnMappingEntry]] = None
    if column_mapping:
        try:
            mapping = [ColumnMappingEntry(**entry) for entry in json.loads(column_mapping)]
        except (json.JSONDecodeError, TypeError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid column_mapping payload: {exc}")

    db = SessionLocal()
    try:
        try:
            parsed = etl.parse_file(content, file.filename, columns_hint, mapping, header_row)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        # Variables the team defined in the template's "New variables" sheet:
        # checked against the catalog (and linked/flagged) before the columns resolve.
        new_variables, new_variable_issues = etl.process_new_variables(db, parsed)
        specs, column_issues = etl.resolve_columns(db, parsed.column_ids, new_variables=new_variables)
        known_sample_types = {st.sample_type_code for st in db.query(SampleType.sample_type_code).all()}
        issues, summary, valid_rows = etl.validate(parsed, specs, known_sample_types)
        summary["new_variables"] = len(new_variables)
        # Uploads only add data: flag anything that already exists so the file is
        # corrected before it ever reaches a reviewer.
        conflicts = etl.find_existing_conflicts(db, parsed.rows, specs)
        issues = new_variable_issues + column_issues + issues + conflicts
        if conflicts:
            valid_rows = max(valid_rows - len({c["row"] for c in conflicts}), 0)

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
                new_variables=new_variables,
            )
        )

        # What the file actually contains, for prefilling the ficha.
        var_ids = [sp.ref_id for sp in specs if sp.kind == 'var' and sp.ref_id]
        detected_matrices, detected_variables = [], []
        if var_ids:
            for name, matrix in (
                db.query(Variable.name, Variable.matrix).filter(Variable.id.in_(var_ids)).all()
            ):
                if name and name not in detected_variables:
                    detected_variables.append(name)
                if matrix and matrix not in detected_matrices:
                    detected_matrices.append(matrix)
        for new_variable in new_variables:
            if new_variable["name"] not in detected_variables:
                detected_variables.append(new_variable["name"])
            if new_variable.get("matrix") and new_variable["matrix"] not in detected_matrices:
                detected_matrices.append(new_variable["matrix"])

        return ValidateResponse(
            detected_matrices=sorted(detected_matrices),
            detected_variables=sorted(detected_variables),
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
async def import_upload(payload: ImportRequest = Body(...), user: CurrentUser = Depends(get_current_user)):
    """Submit a previously validated upload for review.

    This does NOT write to the domain tables. The parsed rows are staged in
    ``submission_row`` under a PENDING ``dataset_submission``; a database_editor
    replays them into the database by approving (see ``routes/submissions``).
    That is what keeps un-approved data out of the catalogue by construction,
    and what makes a rejection a clean discard instead of a deletion.
    """
    stored = etl.upload_store.get(payload.upload_id)
    if not stored:
        raise HTTPException(status_code=404, detail="upload_id no encontrado o expirado. Vuelva a validar el archivo.")

    if stored.error_count > 0:
        raise HTTPException(status_code=409, detail=f"El archivo tiene {stored.error_count} error(es). Corríjalos antes de importar.")
    if stored.warning_count > 0 and not payload.ignore_warnings:
        raise HTTPException(status_code=409, detail=f"El archivo tiene {stored.warning_count} advertencia(s). Reenvíe con ignore_warnings=true para continuar.")

    db = SessionLocal()
    try:
        # Project codes come from the rows: the project itself may not exist yet
        # (it is created by the ETL at approval time), so project_id stays NULL.
        codes: List[str] = []
        names: dict = {}
        for row in stored.rows:
            code = etl.normalize_code(row.get("project_code"))
            if code and code not in names:
                codes.append(code)
                names[code] = (row.get("project_name") or "").strip() or code

        # Validate the ficha now (not at approval) so the uploader sees the error
        # while the form is still in front of them. QC fields stay off-limits here:
        # this endpoint is reachable by a plain editor.
        metadata_payload = None
        if payload.metadata:
            try:
                meta = ProjectMetadataUpsert(**payload.metadata)
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=f"Metadatos inválidos: {exc.errors()[:5]}")
            sent_qc = [f for f in QC_FIELDS if f in meta.model_fields_set]
            if sent_qc:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Los campos de control de calidad los completa el equipo revisor: "
                        f"{', '.join(sent_qc)}."
                    ),
                )
            metadata_payload = jsonable_encoder(meta.model_dump(exclude_unset=True))

        submission = DatasetSubmission(
            project_id=None,
            metadata_payload=metadata_payload,
            project_code=codes[0] if codes else None,
            project_name=names.get(codes[0]) if codes else None,
            project_codes=codes,
            filename=stored.filename,
            column_ids=stored.column_ids,
            new_variables=stored.new_variables or None,
            total_rows=len(stored.rows),
            summary=stored.summary,
            warning_count=stored.warning_count,
            submitted_by_sub=user.sub,
            submitted_by_email=user.email,
            submitted_by_name=user.username,
            organization=getattr(user, "organization", None),
            country=getattr(user, "country", None),
            status="PENDING",
        )
        db.add(submission)
        db.flush()

        db.bulk_save_objects(
            [
                SubmissionRow(
                    submission_id=submission.id,
                    row_number=row.get("_row"),
                    project_code=etl.normalize_code(row.get("project_code")),
                    site_code=(row.get("site_code") or None),
                    sample_code=(row.get("sample_code") or None),
                    data=jsonable_encoder(row),
                )
                for row in stored.rows
            ]
        )
        db.commit()
        etl.upload_store.pop(payload.upload_id)

        label = ", ".join(names.get(c, c) for c in codes) or "tu dataset"

        # Notify the submitter (best-effort; never blocks the submission).
        if user.email:
            send_email(
                user.email,
                "Tu dataset está en revisión — Alliance Soil-GHG Database",
                f"Hola{(' ' + user.username) if user.username else ''},\n\n"
                f"Gracias por subir datos a {label}. Tu dataset fue recibido y está siendo "
                f"revisado por el equipo. Te avisaremos cuando sea aprobado o rechazado.\n\n"
                f"— Alliance Soil-GHG Database",
            )

        # Notify reviewers (database_editor/admin) so a new submission doesn't sit
        # unseen until someone happens to check the queue. Best-effort: Keycloak
        # admin being unconfigured/unreachable must never block this upload.
        try:
            reviewer_emails = keycloak_admin.list_reviewer_emails()
        except Exception:
            reviewer_emails = []
        if reviewer_emails:
            frontend_url = os.getenv("FRONTEND_URL", "").rstrip("/")
            review_link = f"\n\n{frontend_url}/dashboard/logs" if frontend_url else ""
            for email in reviewer_emails:
                send_email(
                    email,
                    f"Nuevo dataset pendiente de revisión — {label}",
                    f"Se recibió un nuevo dataset «{label}» ({user.email or user.username}) "
                    f"que requiere revisión.{review_link}\n\n"
                    f"— Alliance Soil-GHG Database",
                )

        return ImportResponse(
            status="pending_review",
            inserted=stored.summary,
            skipped_rows=0,
            message="Carga enviada a revisión. Los datos se publicarán cuando sean aprobados.",
        )
    except HTTPException:
        # Deliberate 4xx (invalid ficha, QC fields) must reach the client as-is,
        # not be swallowed into a generic 500.
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al enviar la carga a revisión: {exc}")
    finally:
        db.close()
