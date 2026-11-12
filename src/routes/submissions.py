"""Dataset approval / traceability endpoints.

A ``database_editor`` (or admin) reviews uploaded datasets: list PENDING ones and
approve/reject them. The submitter is emailed the outcome.
"""

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import or_

from dependencies.auth import CurrentUser, require_database_editor
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import DatasetSubmission, ProjectMetadata, SubmissionRow
from schemas.metadata import QC_FIELDS
from schemas.submissions import (
    ReviewRequest,
    SubmissionRead,
    SubmissionRowRead,
    SubmissionRowsResponse,
    VariableResolution,
)
from services import etl
from services.email import send_email

router = APIRouter(prefix="/submissions", tags=["Submissions"])


@router.get("", response_model=List[SubmissionRead])
async def list_submissions(
    status: Optional[str] = Query(None, description="Filter by PENDING | APPROVED | REJECTED"),
    limit: int = Query(100, ge=1, le=500),
    _user: CurrentUser = Depends(require_database_editor),
):
    """List dataset submissions (newest first). database_editor / admin only."""
    db = SessionLocal()
    try:
        query = db.query(DatasetSubmission)
        if status:
            query = query.filter(DatasetSubmission.status == status.upper())
        return query.order_by(DatasetSubmission.submitted_at.desc()).limit(limit).all()
    finally:
        db.close()


@router.get("/{submission_id}/rows", response_model=SubmissionRowsResponse)
async def list_submission_rows(
    submission_id: int,
    search: Optional[str] = Query(None, description="Match project / site / sample code"),
    project_code: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    _user: CurrentUser = Depends(require_database_editor),
):
    """Staged rows of a submission, so the reviewer can inspect before deciding."""
    db = SessionLocal()
    try:
        if not db.query(DatasetSubmission.id).filter(DatasetSubmission.id == submission_id).first():
            raise HTTPException(status_code=404, detail="Submission not found")

        query = db.query(SubmissionRow).filter(SubmissionRow.submission_id == submission_id)
        if project_code:
            query = query.filter(SubmissionRow.project_code == project_code)
        if search:
            like = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    SubmissionRow.sample_code.ilike(like),
                    SubmissionRow.site_code.ilike(like),
                    SubmissionRow.project_code.ilike(like),
                )
            )

        total = query.count()
        rows = (
            query.order_by(SubmissionRow.row_number.asc(), SubmissionRow.id.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return SubmissionRowsResponse(
            total=total,
            offset=offset,
            limit=limit,
            rows=[
                SubmissionRowRead(
                    row_number=r.row_number,
                    project_code=r.project_code,
                    site_code=r.site_code,
                    sample_code=r.sample_code,
                    # "_row"/"_source_file" are ETL bookkeeping, not user columns.
                    data={k: v for k, v in (r.data or {}).items() if not k.startswith("_")},
                )
                for r in rows
            ],
        )
    except HTTPException:
        raise
    finally:
        db.close()


def _apply(db, sub: DatasetSubmission, resolutions: Optional[dict] = None) -> dict:
    """Replay the staged rows into the domain tables through the normal ETL.

    Rebuilds the same ``StoredUpload`` the upload flow produced, so approval uses
    one single write path — there is no second, divergent importer. ``resolutions``
    are the reviewer's per-variable decisions for the uploader's new variables.
    """
    rows = (
        db.query(SubmissionRow)
        .filter(SubmissionRow.submission_id == sub.id)
        .order_by(SubmissionRow.row_number.asc(), SubmissionRow.id.asc())
        .all()
    )
    stored = etl.StoredUpload(
        upload_id=f"submission-{sub.id}",
        filename=sub.filename or "carga.xlsx",
        column_ids=list(sub.column_ids or []),
        rows=[r.data for r in rows],
        issues=[],
        summary=sub.summary or {},
        error_count=0,
        warning_count=sub.warning_count or 0,
        new_variables=list(sub.new_variables or []),
    )
    return etl.import_upload(db, stored, resolutions)


def _apply_metadata(db, project_id: int, payload: Optional[dict], owner_email: Optional[str]) -> None:
    """Write the staged ficha onto the project's ``project_metadata`` row.

    Partial upsert: only the fields the uploader actually filled are written, so
    approving a second submission never blanks out metadata set earlier.

    Whoever uploaded the data is always kept in ``authorized_users``. Without this,
    a PRIVATE dataset (the form's default) is visible to nobody but an admin — not
    even to the person who submitted it, nor to the reviewer who approved it.
    """
    if not project_id:
        return
    meta = (
        db.query(ProjectMetadata).filter(ProjectMetadata.project_id == project_id).first()
    )
    if not meta:
        if not payload and not owner_email:
            return
        meta = ProjectMetadata(project_id=project_id)
        db.add(meta)
    for field, value in (payload or {}).items():
        if field in QC_FIELDS or not hasattr(meta, field):
            continue
        setattr(meta, field, value)

    if owner_email:
        current = list(meta.authorized_users or [])
        if not any((e or "").lower() == owner_email.lower() for e in current):
            meta.authorized_users = [*current, owner_email]
    db.flush()


def _review(
    submission_id: int,
    new_status: str,
    notes: Optional[str],
    reviewer: CurrentUser,
    variable_resolutions: Optional[List[VariableResolution]] = None,
) -> DatasetSubmission:
    db = SessionLocal()
    try:
        sub = db.query(DatasetSubmission).filter(DatasetSubmission.id == submission_id).first()
        if not sub:
            raise HTTPException(status_code=404, detail="Submission not found")
        if sub.status != "PENDING":
            raise HTTPException(status_code=409, detail=f"Submission already {sub.status.lower()}")

        # Approving is what publishes the data. It runs in the same transaction as
        # the status change, so a failed import leaves the submission PENDING
        # instead of marking it approved with nothing written.
        # A submission tied to a dataset before approval adds data to one that already
        # exists (a new dataset is only created by approving it).
        is_extension = sub.project_id is not None

        if new_status == "APPROVED":
            try:
                known_codes = {d.get("code") for d in (sub.new_variables or [])}
                unknown = [r.code for r in (variable_resolutions or []) if r.code not in known_codes]
                if unknown:
                    raise etl.VariableResolutionError(
                        f"This submission has no new variable with code: {', '.join(unknown)}."
                    )
                resolutions = {
                    r.code: r.model_dump(exclude_unset=True) for r in (variable_resolutions or [])
                }
                result = _apply(db, sub, resolutions)
            except etl.VariableResolutionError as exc:
                db.rollback()
                raise HTTPException(status_code=422, detail={"message": str(exc)})
            except etl.ImportConflict as conflict:
                # Another submission was approved first and now owns these values.
                # Stay PENDING and tell the reviewer exactly which rows collide.
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "No se puede aprobar: algunas filas ya existen en la base de datos. "
                            "Las cargas solo agregan datos nuevos."
                        ),
                        "conflicts": conflict.issues[:50],
                        "conflict_count": len(conflict.issues),
                    },
                )
            sub.inserted = result["inserted"]
            affected = result.get("projects_affected") or []
            if affected:
                sub.project_id = affected[0].get("id")
                sub.project_name = sub.project_name or affected[0].get("name")
                # The ficha was filled at upload time, when the project did not
                # exist yet. Now it does, so apply it (partial: only sent fields).
                # The uploader of a new dataset keeps access to it; someone adding data to a
                # dataset that was already there is not put on its access list by doing so.
                _apply_metadata(
                    db, sub.project_id, sub.metadata_payload, None if is_extension else sub.submitted_by_email
                )

        sub.status = new_status
        sub.reviewed_at = datetime.now(timezone.utc)
        sub.reviewed_by_email = reviewer.email
        sub.reviewed_by_name = reviewer.username
        sub.review_notes = notes
        db.commit()
        db.refresh(sub)

        # Notify the submitter (best-effort).
        if sub.submitted_by_email:
            dataset = sub.project_name or sub.project_code or "tu dataset"
            if new_status == "APPROVED":
                subject = "Tu dataset fue aprobado — Alliance Soil-GHG Database"
                body = (
                    f"Hola{(' ' + sub.submitted_by_name) if sub.submitted_by_name else ''},\n\n"
                    f"Tu dataset «{dataset}» fue **aprobado** y ya forma parte de la plataforma.\n"
                    f"{('Notas del revisor: ' + notes) if notes else ''}\n\n"
                    f"— Alliance Soil-GHG Database"
                )
            else:
                subject = "Tu dataset fue rechazado — Alliance Soil-GHG Database"
                body = (
                    f"Hola{(' ' + sub.submitted_by_name) if sub.submitted_by_name else ''},\n\n"
                    f"Tu dataset «{dataset}» fue **rechazado** tras la revisión.\n"
                    f"{('Motivo: ' + notes) if notes else 'Contacta al equipo para más detalles.'}\n\n"
                    f"— Alliance Soil-GHG Database"
                )
            send_email(sub.submitted_by_email, subject, body)

        return sub
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post("/{submission_id}/approve", response_model=SubmissionRead)
async def approve_submission(
    submission_id: int,
    payload: ReviewRequest = Body(default=ReviewRequest()),
    user: CurrentUser = Depends(require_database_editor),
):
    """Approve a submission: replays its staged rows into the database, applies the dataset
    ficha and emails the submitter. This is the step that publishes the data. Returns
    409 if any row already exists.
    """
    return _review(submission_id, "APPROVED", payload.notes, user, payload.variable_resolutions)


@router.post("/{submission_id}/reject", response_model=SubmissionRead)
async def reject_submission(
    submission_id: int,
    payload: ReviewRequest = Body(default=ReviewRequest()),
    user: CurrentUser = Depends(require_database_editor),
):
    """Reject a submission. Nothing is written to the database; the staged rows are kept as
    evidence and the submitter is emailed the reason.
    """
    return _review(submission_id, "REJECTED", payload.notes, user)
