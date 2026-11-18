"""The office's live stream for the page (`app/live.py`).

`GET /stream` stays open and sends a line for each notice the database sends:
a new run event, thread message or run control, or a run's status changed.
The page redraws what a line names; it reads the content through its usual
routes. Owner only, like every route on this router.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.live import LiveHub, event_stream


def add_live_routes(router: APIRouter) -> None:
    @router.get("/stream")
    async def stream(request: Request) -> StreamingResponse:
        hub = getattr(request.app.state, "live", None)
        if not isinstance(hub, LiveHub):
            raise HTTPException(status_code=503, detail="live_unavailable")
        return StreamingResponse(
            event_stream(hub, request.is_disconnected),
            media_type="text/event-stream",
            # Not cached or held back by a proxy: each line is sent as it comes.
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
