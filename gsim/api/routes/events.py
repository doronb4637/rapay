"""
Live event stream: core's inbound callbacks -> the UI's log panels.

This is the one place that IS `async def`, because it does no core work -- it
only drains an `asyncio.Queue` that `EventBus.publish` fills from whatever
thread core happened to call back on.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from gsim.core_gateway import get_runtime

router = APIRouter(tags=["events"])


@router.websocket("/ws/events")
async def events(websocket: WebSocket) -> None:
    await websocket.accept()
    runtime = get_runtime()
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    runtime.events.subscribe(asyncio.get_running_loop(), queue)
    try:
        # Backfill so a reconnecting client is not missing the current state.
        # Behaviours ride along: they keep firing across a dropped socket, so a
        # client that reconnected without them would show an idle Behaviours
        # panel while traffic was still going out.
        await websocket.send_json({
            "type": "snapshot",
            "connections": runtime.list(),
            "behaviours": runtime.behaviours.list(),
        })
        while True:
            await websocket.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        runtime.events.unsubscribe(queue)
