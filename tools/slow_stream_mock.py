"""Slow-stream mock of the Responses API for SIGTERM drain verification.

Emits a few ``response.output_text.delta`` SSE frames on
``POST /openai/v1/responses`` and then deliberately never finishes, so an
in-flight ``/chat/stream`` request stays open until uvicorn's
``--timeout-graceful-shutdown`` cancels it (Day 23 evidence). Auth is
ignored: point the container at this mock with dummy credentials.

Run (host side, from the repo root):

    uv run uvicorn tools.slow_stream_mock:app --host 0.0.0.0 --port 9999

Binds 0.0.0.0 so ``host.docker.internal`` can reach it; local sessions
only — stop it when the verification run is over.
"""

import asyncio
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from openai.types.responses import ResponseTextDeltaEvent

app = FastAPI()


def _delta_frame(sequence_number: int, text: str) -> str:
    # Built through the pinned SDK's own model: if the SDK's schema and this
    # payload ever drift apart, this line fails loudly instead of the client
    # silently dropping frames.
    event = ResponseTextDeltaEvent(
        type="response.output_text.delta",
        item_id="item_0",
        output_index=0,
        content_index=0,
        delta=text,
        sequence_number=sequence_number,
        logprobs=[],
    )
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def _hold_forever() -> AsyncIterator[bytes]:
    for n in range(3):
        yield _delta_frame(n, f"tick {n} ").encode()
        await asyncio.sleep(1.0)
    # Never set: the response stays in-flight until the client goes away.
    await asyncio.Event().wait()


@app.post("/openai/v1/responses")
async def responses() -> StreamingResponse:
    return StreamingResponse(_hold_forever(), media_type="text/event-stream")
