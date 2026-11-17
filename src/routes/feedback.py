"""Feedback from the people using the platform (the floating "Feedback" button).

There is no table behind this: each message is emailed to the people in
``FEEDBACK_TO`` (comma-separated) and that is all. It is open to anonymous
callers on purpose — someone who cannot sign in is exactly who needs to say so —
which is why it is rate limited and has a honeypot field.
"""

import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from dependencies.auth import CurrentUser, get_optional_user
from services import email

logger = logging.getLogger("mfl.feedback")

router = APIRouter(prefix="/feedback", tags=["Feedback"])

FeedbackType = Literal["bug", "suggestion", "question", "other"]
TYPE_LABELS = {"bug": "Error", "suggestion": "Suggestion", "question": "Question", "other": "Other"}

MAX_MESSAGE = 3000
MIN_MESSAGE = 5
_EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")

# Sliding-window limits. One API process serves the site, so memory is enough.
# The caller is the signed-in user when there is one, otherwise the client address;
# behind the Next proxy that address can be the proxy's own, so anonymous callers
# also share a lower global budget.
PER_CALLER = (5, 3600)  # 5 messages an hour
ANONYMOUS_TOTAL = (30, 3600)  # 30 an hour from everyone anonymous together

_hits: Dict[str, Deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def _allow(key: str, limit: int, window: int, now: float) -> bool:
    hits = _hits[key]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        return False
    hits.append(now)
    return True


def _too_many(caller: str, anonymous: bool) -> bool:
    now = time.monotonic()
    with _lock:
        if not _allow(f"caller:{caller}", *PER_CALLER, now):
            return True
        if anonymous and not _allow("anonymous", *ANONYMOUS_TOTAL, now):
            return True
    return False


class FeedbackRequest(BaseModel):
    type: FeedbackType = "other"
    message: str = Field(max_length=MAX_MESSAGE)
    contact_email: Optional[str] = Field(default=None, max_length=254)
    page: Optional[str] = Field(default=None, max_length=300)
    language: Optional[str] = Field(default=None, max_length=10)
    # Honeypot: hidden in the form, so a person leaves it empty and a bot fills it in.
    website: Optional[str] = None

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        value = value.strip()
        if len(value) < MIN_MESSAGE:
            raise ValueError(f"The message must have at least {MIN_MESSAGE} characters")
        return value

    @field_validator("contact_email")
    @classmethod
    def _contact_email(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        if not value:
            return None
        if not _EMAIL_RE.match(value):
            raise ValueError("That does not look like an email address")
        return value

    @field_validator("page", "language")
    @classmethod
    def _one_line(cls, value: Optional[str]) -> Optional[str]:
        # These end up in the body of an email: keep them to one line.
        value = " ".join((value or "").split())
        return value or None


class FeedbackResponse(BaseModel):
    status: Literal["received"] = "received"


def _recipients() -> List[str]:
    return [a.strip() for a in os.getenv("FEEDBACK_TO", "").split(",") if a.strip()]


def _body(payload: FeedbackRequest, user_agent: Optional[str], user: Optional[CurrentUser]) -> str:
    lines = [
        payload.message,
        "",
        "—",
        f"Type: {TYPE_LABELS[payload.type]}",
        f"Page: {payload.page or '-'}",
        f"Language: {payload.language or '-'}",
        f"Browser: {' '.join((user_agent or '-').split())[:300]}",
    ]
    if payload.contact_email:
        # Who is writing is only recorded when they chose to be contactable.
        who = f"{user.username} ({', '.join(user.roles) or 'no roles'})" if user and user.username else "not signed in"
        lines += [f"Reply to: {payload.contact_email}", f"Account: {who}"]
    return "\n".join(lines)


@router.post("", response_model=FeedbackResponse)
def send_feedback(
    payload: FeedbackRequest,
    request: Request,
    user: Optional[CurrentUser] = Depends(get_optional_user),
):
    """Email a piece of feedback to the team. Public: no sign-in needed."""
    if payload.website:
        # A bot: pretend it worked so it learns nothing.
        return FeedbackResponse()

    recipients = _recipients()
    if not recipients:
        raise HTTPException(status_code=503, detail="Feedback is not set up on this server.")

    if user and user.sub:
        caller = f"user:{user.sub}"
    else:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        caller = f"ip:{forwarded or (request.client.host if request.client else 'unknown')}"
    if _too_many(caller, anonymous=not (user and user.sub)):
        raise HTTPException(status_code=429, detail="Too many messages — please try again later.")

    subject = f"[GHG DataHub feedback] {TYPE_LABELS[payload.type]}: {payload.message[:60]}"
    subject = " ".join(subject.split())
    body = _body(payload, request.headers.get("user-agent"), user)

    sent = [email.send_email(to, subject, body, reply_to=payload.contact_email) for to in recipients]
    if not any(sent):
        # Nothing was mailed. Without SMTP (dev, or a server not yet set up) the helper only
        # logs; keep the message in the log too so it is not lost.
        if email.is_configured():
            logger.error("Feedback could not be emailed:\n%s", body)
            raise HTTPException(status_code=502, detail="The message could not be sent — please try again later.")
        logger.warning("SMTP not configured; feedback kept only in this log:\n%s", body)
    return FeedbackResponse()
