"""
Live event stream: core's inbound callbacks -> the UI's log panels.

This is the one place that IS `async def`, because it does no core work -- it
only drains an `asyncio.Queue` that `EventBus.publish` fills from whatever
thread core happened to call back on.

**Nothing is sampled or dropped on the way out.** A 1ms behaviour really is
~1000 events a second per direction, and the console has to show that: it is
the instrument this project's timing is diagnosed with, and a feed sampled at
60Hz puts consecutive rows ~16.7ms apart, which is indistinguishable from the
scheduler itself being stuck at the Windows timer tick. Sampling was tried and
reverted for exactly that reason.

What makes full fidelity affordable is coalescing, which costs no fidelity at
all: one `send_json` is awaited at a time, and everything the publisher queued
while that write was in flight goes out together in the next frame. The frame
rate is therefore whatever the client can actually absorb -- one frame per
message when it keeps up, one frame per hundred when it does not -- and it
needs no configured rate, no timer, and no guess about how fast a browser is.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from gsim.core_gateway import get_runtime

router = APIRouter(tags=["events"])

#: The event types that may be merged into one frame. Only log entries: they
#: are homogeneous, order-preserving and the only ones that arrive in volume.
_MESSAGE_TYPES = frozenset({"message.sent", "message.received"})


async def _drain(queue: asyncio.Queue) -> list[dict[str, Any]]:
    """Block for one event, then take everything else already waiting."""
    events = [await queue.get()]
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


def _coalesce(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge runs of consecutive message events into single `messages` frames.

    Only CONSECUTIVE ones, and everything else keeps its place in the sequence.
    That ordering is load-bearing: `logs.cleared` tells the client to empty a
    pane, so entries that were published before it must not be delivered after
    it, or the pane refills with exactly what was just cleared. Same for
    `connection.deleted`, which drops that connection's rows.
    """
    frames: list[dict[str, Any]] = []
    batch: dict[str, Any] | None = None
    for event in events:
        if event["type"] in _MESSAGE_TYPES:
            if batch is None:
                batch = {"type": "messages", "entries": []}
                frames.append(batch)
            batch["entries"].append(event["entry"])
        else:
            batch = None         # anything else ends the run
            frames.append(event)
    return frames


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
        # panel while traffic was still going out. Filters likewise -- they keep
        # dropping messages across a reconnect, and a console that reconnected
        # without them would show a quiet Received pane with nothing on screen
        # saying why.
        await websocket.send_json({
            "type": "snapshot",
            "connections": runtime.list(),
            "behaviours": runtime.behaviours.list(),
            "filters": runtime.filters.list(),
        })
        while True:
            for frame in _coalesce(await _drain(queue)):
                await websocket.send_json(frame)
    except WebSocketDisconnect:
        pass
    finally:
        runtime.events.unsubscribe(queue)
