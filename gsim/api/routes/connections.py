"""
Connections Manager endpoints (the yellow box + its black button bar).

Every handler here is `def`, not `async def`, and that is deliberate: core's
public API is blocking (each call marshals onto core's own background loop via
`await_coroutine(...)` and waits on the result). Declaring these `async def`
would run them ON the ASGI event loop and stall every other request, including
the WebSocket that streams the logs. As plain `def`, Starlette runs each in its
threadpool, where blocking is exactly what's expected.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from gsim.api.models import ConnectionCreate, ConnectionUpdate
from gsim.core_gateway import get_runtime

router = APIRouter(prefix="/api/connections", tags=["connections"])


@router.get("")
def list_connections() -> list[dict[str, Any]]:
    """Everything the yellow box renders: name, on/off state, peers."""
    return get_runtime().list()


@router.post("", status_code=status.HTTP_201_CREATED)
def create_connection(request: ConnectionCreate) -> dict[str, Any]:
    """Create one connection from the modal's inputs.

    The flow, end to end:

      1. Pydantic enforces GSim's stricter contract (`structures` mandatory,
         per-peer `unitCode` mandatory and unique, side valid for protocol) and
         returns a 422 with field paths the modal can highlight inline.
      2. `to_core_config()` renders those inputs into core's JSON config shape
         -- the flat `{protocol, side, ip, local_ip, unitCode, connections{...}}`
         dict, with `Structures` and any echo keys alongside.
      3. `runtime.create()` hands that dict to `ConnectionManager.create()`,
         which validates it (`ConnectionConfig.from_json`), imports the
         `Structures` modules so the GLOBAL REGISTRY is populated *before* the
         connection object exists, and instantiates the protocol class.
      4. Receive handlers are registered per (peer, opcode) and, if requested,
         the connection is started.

    A bad config surfaces as core's own `ValueError` -> 400 here, so the modal
    can show the real reason ("connections 'A' and 'B' both use unitCode 7")
    rather than a generic failure.
    """
    config = request.to_core_config()
    try:
        record = get_runtime().create(request.name, config, autostart=request.autostart)
    except ValueError as exc:
        # ConnectionConfig.from_json / install-time validation.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ModuleNotFoundError as exc:
        # A `Structures` entry that does not resolve under IRS.Structures.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"structures module not found: {exc.name}",
        ) from exc
    except OSError as exc:
        # autostart=True and the port is taken / address unusable.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"could not open the connection: {exc}",
        ) from exc
    return record.as_dict()


@router.get("/{connection_id}")
def get_connection(connection_id: str) -> dict[str, Any]:
    return _record(connection_id).as_dict()


@router.put("/{connection_id}")
def update_connection(connection_id: str, request: ConnectionUpdate) -> dict[str, Any]:
    """'Edit'. Rebuilds rather than mutates: `ConnectionConfig` is
    `frozen=True, slots=True` and a live `Connection` caches state derived from
    it, so in-place edits would desync those caches. Running state is
    preserved across the rebuild."""
    _record(connection_id)
    try:
        record = get_runtime().replace(connection_id, request.name, request.to_core_config())
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return record.as_dict()


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(connection_id: str) -> None:
    _record(connection_id)
    get_runtime().delete(connection_id)


@router.post("/{connection_id}/start")
def start_connection(connection_id: str) -> dict[str, Any]:
    _record(connection_id)
    try:
        return get_runtime().start(connection_id).as_dict()
    except OSError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{connection_id}/stop")
def stop_connection(connection_id: str) -> dict[str, Any]:
    _record(connection_id)
    return get_runtime().stop(connection_id).as_dict()


def _record(connection_id: str):
    try:
        return get_runtime().get(connection_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
