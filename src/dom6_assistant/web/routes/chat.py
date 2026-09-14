"""AI chat streaming route.

POST /api/chat  →  SSE stream of {"delta": "..."} events
DELETE /api/chat/history  →  clear history (history is client-managed)

The sync backend.stream() iterator runs in a thread-pool thread and feeds
an asyncio.Queue; the async generator drains that queue for EventSourceResponse.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from dom6_assistant.ai.backend import get_backend

router = APIRouter(prefix="/chat", tags=["chat"])

_DEFAULT_SYSTEM = (
    "You are an expert Dominions 6 strategist. "
    "Give concise, practical tactical and strategic advice. "
    "When uncertain, explain your reasoning and acknowledge uncertainty."
)


class Message(BaseModel):
    role: str    # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    backend: str | None = None
    system_prompt: str | None = None
    history: list[Message] = []


async def _stream_from_sync(backend, system: str, messages: list[dict]) -> AsyncGenerator:
    """Bridge a synchronous Iterator[str] into an async generator for SSE."""
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    abandoned = threading.Event()

    def send(item) -> bool:
        if abandoned.is_set():
            return False
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            return False
        return True

    def producer() -> None:
        try:
            for chunk in backend.stream(system, messages):
                if not send(chunk):
                    return
        except Exception as exc:
            send(exc)
        finally:
            send(None)

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                yield {"data": json.dumps({"error": str(item)})}
                break
            yield {"data": json.dumps({"delta": item})}

        yield {"data": "[DONE]"}
    finally:
        abandoned.set()


@router.post("")
async def chat(body: ChatRequest):
    try:
        backend = get_backend(name=body.backend)
    except (ImportError, EnvironmentError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    system = body.system_prompt or _DEFAULT_SYSTEM
    messages = [{"role": m.role, "content": m.content} for m in body.history]
    messages.append({"role": "user", "content": body.message})

    return EventSourceResponse(_stream_from_sync(backend, system, messages))
