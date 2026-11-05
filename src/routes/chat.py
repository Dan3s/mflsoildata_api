"""Q&A chat assistant endpoint.

Thin HTTP layer over ``services.gemini_chat`` — the tool-calling loop and the
model call itself live there so they stay reusable outside a request (tests,
an eventual MCP server exposing the same ``services.chat_tools.TOOLS``).
"""

from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependencies.auth import CurrentUser, get_current_user
from mflsoildata_orm.database import SessionLocal
from services import gemini_chat

router = APIRouter(prefix="/chat", tags=["Chat"])


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class ChatRequest(BaseModel):
    message: str
    history: List[ChatTurn] = []


class ChatResponse(BaseModel):
    reply: str


@router.post("/message", response_model=ChatResponse)
async def send_message(payload: ChatRequest, user: CurrentUser = Depends(get_current_user)):
    """Answer a question about the platform's data. Stateless: the client resends
    the conversation history alongside each new message."""
    db = SessionLocal()
    try:
        reply = gemini_chat.answer(
            payload.message,
            [{"role": turn.role, "text": turn.text} for turn in payload.history],
            db,
            user,
        )
        return ChatResponse(reply=reply)
    except gemini_chat.ChatError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        db.close()
