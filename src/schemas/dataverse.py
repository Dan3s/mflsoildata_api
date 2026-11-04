"""Pydantic schemas for the Dataverse import preview (see services.dataverse)."""

from typing import Any, List, Optional

from pydantic import BaseModel


class DataverseFileInfo(BaseModel):
    id: int
    filename: str
    content_type: Optional[str] = None
    size: Optional[int] = None
    restricted: bool = False
    # When Dataverse ingested the upload (e.g. Stata/SPSS -> .tab), the name it
    # was originally uploaded under — shown so the user recognizes their file.
    original_filename: Optional[str] = None
    # Depositor-assigned tags (e.g. "Documentation", "Data", "Code") — optional,
    # most datasets never set these, so treat as a nice-to-have badge, not a
    # guarantee.
    categories: Optional[List[str]] = None


class DataversePreviewResponse(BaseModel):
    doi: str
    persistent_id: str
    # Best-effort display citation, not authoritative.
    citation: Optional[str] = None
    license: Optional[str] = None
    license_uri: Optional[str] = None
    # Ficha-shaped dict (same keys as ProjectMetadataUpsert) to prefill the
    # upload wizard's metadata form.
    ficha: dict
    files: List[DataverseFileInfo]
    # The dataset has a Guestbook attached (Dataverse-side, e.g. required
    # contact details before download): downloading ANY file below 400s until
    # that form is answered — fetch its questions from GET /dataverse/guestbook/{id}
    # and resubmit the download with a GuestbookResponseInput.
    requires_guestbook: bool = False
    guestbook_id: Optional[int] = None


class GuestbookQuestion(BaseModel):
    id: int
    question: str
    required: bool = False
    type: str = "text"


class GuestbookInfo(BaseModel):
    id: int
    name: str
    email_required: bool = False
    name_required: bool = False
    institution_required: bool = False
    position_required: bool = False
    custom_questions: List[GuestbookQuestion] = []


class GuestbookAnswer(BaseModel):
    id: int
    value: str


class GuestbookResponseInput(BaseModel):
    """What the frontend collects from the user before a guestbook-gated download."""

    name: Optional[str] = None
    email: Optional[str] = None
    institution: Optional[str] = None
    position: Optional[str] = None
    answers: List[GuestbookAnswer] = []


class DataverseDownloadRequest(BaseModel):
    file_id: int
    filename: Optional[str] = None
    token: Optional[str] = None
    guestbook_response: Optional[GuestbookResponseInput] = None
