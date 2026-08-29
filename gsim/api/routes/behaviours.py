"""
Behaviours: scheduled sending, configured per message route.

Same threading rule as the rest of the API (`def`, never `async def`): although
these handlers do not themselves block on core, `PUT` normalises a payload and
resolves a schema, and the engine they hand it to drives `runtime.send()` on its
own worker threads.

`PUT` is an upsert keyed by `(connection, unit_name, op_code)` rather than a
`POST` that mints a new resource each call: one message route can only have one
behaviour, because two schedules on one route would silently double its send
rate (see `core_gateway/behaviours.py`).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from gsim.api.models import BehaviourRequest
from gsim.core_gateway import (
    IRSAmbiguousError,
    get_runtime,
    prepare_message,
)

router = APIRouter(prefix="/api", tags=["behaviours"])


@router.get("/behaviours")
def list_behaviours() -> list[dict[str, Any]]:
    """Every configured behaviour, across every connection.

    Process-wide for the same reason the console is: a schedule keeps firing
    while you look at a different connection, and a view that hid it would be
    the one place the UI lies about what it is doing.
    """
    return get_runtime().behaviours.list()


@router.put("/connections/{connection_name}/behaviours")
def set_behaviour(connection_name: str, request: BehaviourRequest) -> dict[str, Any]:
    """Create or replace the behaviour on one route.

    The payload is normalised HERE, once, by the same `prepare_message` the manual
    send path uses -- letting IRS's `fill()` supply every absent field and
    reconciling counted-array lengths. Doing it at configure time rather than per
    tick means a payload that could never encode fails in this request, where the
    modal can show why, instead of logging the identical error forever on a worker
    thread. What is stored is the message's `to_dict()` form, which feeds straight
    back into `prepare_message` on every tick.
    """
    runtime = get_runtime()
    try:
        record = runtime.get(connection_name)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        # Scoped by destination, exactly as `POST /send` is: the same opcode may
        # mean different layouts on two links. Building the real message here is
        # also the validation -- a payload that could never encode fails now.
        prepared = prepare_message(record.own_unit_code, request.op_code,
                                   record.structures_for(request.unit_name),
                                   request.payload)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IRSAmbiguousError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        # A value IRS refuses, e.g. a number naming no member of an enum.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if request.unit_name not in record.peers():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"unknown connected unit {request.unit_name!r}; "
                   f"known: {sorted(record.peers())}",
        )

    try:
        behaviour = runtime.behaviours.set(
            connection_name=connection_name,
            unit_name=request.unit_name,
            op_code=request.op_code,
            kind=request.kind,
            payload=prepared.payload,
            interval=request.interval,
            message_name=prepared.name,
            enabled=request.enabled,
        )
    except ValueError as exc:
        # Unknown kind, or an interval below the engine's floor.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return behaviour.as_dict()


@router.post("/behaviours/{behaviour_id}/start")
def start_behaviour(behaviour_id: str) -> dict[str, Any]:
    """Enable a behaviour. It only actually fires while its connection is also
    running -- starting one on a stopped connection arms it, and it begins when
    the connection does."""
    return _enable(behaviour_id, True)


@router.post("/behaviours/{behaviour_id}/stop")
def stop_behaviour(behaviour_id: str) -> dict[str, Any]:
    """Disable a behaviour but keep it configured, so it can be resumed without
    re-entering its payload and interval."""
    return _enable(behaviour_id, False)


@router.delete("/behaviours/{behaviour_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_behaviour(behaviour_id: str) -> None:
    try:
        get_runtime().behaviours.delete(behaviour_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _enable(behaviour_id: str, enabled: bool) -> dict[str, Any]:
    try:
        return get_runtime().behaviours.set_enabled(behaviour_id, enabled).as_dict()
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
