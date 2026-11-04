"""Tool-calling loop against the Gemini API for the Q&A chat assistant.

Plain REST via ``requests`` rather than the official ``google-genai`` SDK: that
SDK hard-requires httpx>=0.28/anyio>=4, which breaks this project's pinned
FastAPI 0.104/Starlette 0.27 stack (TestClient stops working). REST keeps the
existing dependency tree untouched.

Model: gemini-3.5-flash-lite — free of charge on the Google AI Studio tier as
of 2026-10 (see https://ai.google.dev/gemini-api/docs/pricing). Swap
GEMINI_MODEL if that changes.
"""

import os
from typing import Optional

import requests

from services import chat_tools

GEMINI_API_KEY = os.environ.get("GOOGLE_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

SYSTEM_INSTRUCTION = (
    "You are the MFL Soil Data assistant, embedded in a soil and greenhouse-gas "
    "research data platform. Answer questions about the data ONLY by calling the "
    "tools provided — never invent numbers or datasets. The tools only ever see "
    "clearly-labelled TEST/demo datasets, never the platform's real research "
    "data — if asked, say the assistant currently only has test data to answer "
    "from. If a tool returns no match or an error, say so plainly instead of "
    "guessing. Keep answers short and concrete (a sentence or two plus the key "
    "figures). Reply in the same language the user wrote in."
)

MAX_TOOL_ROUNDS = 4
REQUEST_TIMEOUT = 45


class ChatError(Exception):
    """Raised for anything that stops the assistant producing a reply."""


def _call_gemini(contents: list[dict]) -> dict:
    if not GEMINI_API_KEY:
        raise ChatError("GOOGLE_API_KEY is not configured on the server.")
    try:
        response = requests.post(
            GEMINI_URL,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
                "contents": contents,
                "tools": [{"function_declarations": chat_tools.GEMINI_TOOL_DECLARATIONS}],
                "tool_config": {"function_calling_config": {"mode": "auto"}},
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        # A network-level failure (timeout, connection reset, ...) — e.g. the
        # free tier being slow/overloaded — must surface as a clean ChatError
        # (-> 502), not an unhandled exception (-> bare 500).
        raise ChatError(f"Could not reach Gemini: {exc}") from exc
    if not response.ok:
        raise ChatError(f"Gemini API error {response.status_code}: {response.text[:500]}")
    try:
        return response.json()
    except ValueError as exc:
        raise ChatError("Gemini returned a response that wasn't valid JSON.") from exc


def answer(message: str, history: list[dict], db, user) -> str:
    """Run the tool-calling loop and return the assistant's final text answer.

    ``history`` is a flat list of ``{"role": "user" | "assistant", "text": str}``
    sent by the client on every request — there is no server-side session state
    yet (v1 is stateless per request; the client resends the transcript).
    """
    contents = [
        {
            "role": "model" if turn["role"] == "assistant" else "user",
            "parts": [{"text": turn["text"]}],
        }
        for turn in history
    ]
    contents.append({"role": "user", "parts": [{"text": message}]})

    for _ in range(MAX_TOOL_ROUNDS):
        data = _call_gemini(contents)
        candidates = data.get("candidates") or []
        if not candidates:
            raise ChatError("Gemini returned no candidates.")
        parts = candidates[0].get("content", {}).get("parts", [])

        function_calls = [p["functionCall"] for p in parts if "functionCall" in p]
        if not function_calls:
            text = "".join(p.get("text", "") for p in parts).strip()
            return text or "I don't have an answer for that."

        # Echo the model's own turn back, then answer every call it made in one go
        # (Gemini, like Claude, may issue several tool calls in a single turn).
        contents.append({"role": "model", "parts": parts})
        response_parts = []
        for call in function_calls:
            name = call.get("name")
            args = call.get("args") or {}
            tool_fn = chat_tools.TOOLS.get(name)
            if not tool_fn:
                result = {"error": f"Unknown tool '{name}'"}
            else:
                try:
                    result = tool_fn(db, user, **args)
                except Exception as exc:  # noqa: BLE001 — a bad tool call must
                    # never 500 the whole request. The model hallucinating an
                    # argument our tool doesn't accept (e.g. a "city" filter
                    # that doesn't exist) raises a plain TypeError here; feeding
                    # it back as an error lets the model recover and tell the
                    # user plainly, instead of the request crashing outright.
                    result = {"error": f"Tool '{name}' failed: {exc}"}
            # Gemini's FunctionResponse.response is a Struct (JSON object) — a
            # tool that returns a bare list (e.g. list_datasets) must be wrapped,
            # or the API 400s with "Proto field is not repeating, cannot start list".
            if not isinstance(result, dict):
                result = {"result": result}
            response_parts.append({"functionResponse": {"name": name, "response": result}})
        contents.append({"role": "user", "parts": response_parts})

    return "I couldn't find an answer within the allowed number of tool calls — try rephrasing."
