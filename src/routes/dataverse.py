"""Dataverse integration: browse/preview a dataset by DOI, and download its
files — either straight to the user's computer or into the upload wizard's
mapping pipeline. Both paths share one download call (`POST /file/download`,
any authenticated user, with Guestbook support) — the frontend just decides
what to do with the bytes it gets back.

See services.dataverse for the Native/Data Access API calls. The upload-wizard
path never infers column mappings: the file it hands back to the frontend
feeds the exact same /upload/validate mapping flow any other file goes through.
"""

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response

from dependencies.auth import CurrentUser, get_current_user
from schemas.dataverse import (
    DataverseDownloadRequest,
    DataverseFileInfo,
    DataversePreviewResponse,
    GuestbookInfo,
)
from services import dataverse as dataverse_service

router = APIRouter(prefix="/dataverse", tags=["Dataverse"])


@router.get("/preview", response_model=DataversePreviewResponse)
async def preview_dataset(
    doi: str = Query(..., description="DOI del dataset (con o sin el prefijo 'doi:')"),
    token: Optional[str] = Query(None, description="Token de Dataverse, solo si el dataset es restringido"),
    _user: CurrentUser = Depends(get_current_user),
):
    """Metadata + file list for a Dataverse dataset — used both to prefill the
    upload wizard and to browse/download files directly."""
    try:
        result = dataverse_service.fetch_dataset(doi, token)
    except dataverse_service.DataverseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except dataverse_service.DataverseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return DataversePreviewResponse(
        doi=result["doi"],
        persistent_id=result["persistent_id"],
        citation=result["citation"],
        license=result["license"],
        license_uri=result["license_uri"],
        ficha=result["ficha"],
        files=[DataverseFileInfo(**f) for f in result["files"]],
        requires_guestbook=result["requires_guestbook"],
        guestbook_id=result["guestbook_id"],
    )


@router.get("/guestbook/{guestbook_id}", response_model=GuestbookInfo)
async def get_guestbook(
    guestbook_id: int,
    token: Optional[str] = Query(None),
    _user: CurrentUser = Depends(get_current_user),
):
    """The Guestbook's required-field flags and custom questions, to render the
    exact form this dataset configured before a gated download."""
    try:
        result = dataverse_service.fetch_guestbook(guestbook_id, token)
    except dataverse_service.DataverseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except dataverse_service.DataverseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return GuestbookInfo(**result)


@router.post("/file/download")
async def download_file_to_computer(
    payload: DataverseDownloadRequest = Body(...),
    _user: CurrentUser = Depends(get_current_user),
):
    """Download a file straight to the user's computer — any authenticated user,
    not just editors, since this never touches our own database. Supports a
    Guestbook response (POSTed to Dataverse) when the dataset requires one."""
    guestbook_response = None
    if payload.guestbook_response is not None:
        guestbook_response = {
            "name": payload.guestbook_response.name,
            "email": payload.guestbook_response.email,
            "institution": payload.guestbook_response.institution,
            "position": payload.guestbook_response.position,
            "answers": [a.model_dump() for a in payload.guestbook_response.answers],
        }
        guestbook_response = {k: v for k, v in guestbook_response.items() if v not in (None, [])}

    try:
        content, dv_filename, content_type = dataverse_service.fetch_datafile(
            payload.file_id, payload.token, guestbook_response
        )
    except dataverse_service.DataverseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except dataverse_service.DataverseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    final_name = dv_filename or payload.filename or f"dataverse_file_{payload.file_id}"
    return Response(
        content=content,
        media_type=content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{final_name}"'},
    )
