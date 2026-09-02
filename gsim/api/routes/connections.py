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

from gsim.api.models import ConnectionCreate, ConnectionImport, ConnectionUpdate
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
         `Structures` modules (each under its own namespace) so the registry is populated *before* the
         connection object exists, and instantiates the protocol class.
      4. Receive handlers are registered per (peer, opcode) and, if requested,
         the connection is started.

    A bad config surfaces as core's own `ValueError` -> 400 here, so the modal
    can show the real reason ("connections 'A' and 'B' both use unitCode 7")
    rather than a generic failure.
    """
    return _create_or_raise(request.name, request.to_core_config(), request.autostart)


@router.post("/import", status_code=status.HTTP_201_CREATED)
def import_connection(request: ConnectionImport) -> dict[str, Any]:
    """One entry of a Save/Load session file (see `ConnectionImport`'s
    docstring for why this bypasses `ConnectionCreate` and its stricter
    contract entirely -- `config` is already core-shaped). The frontend calls
    this once per saved connection when the user picks a file to Load."""
    return _create_or_raise(request.name, request.config, request.autostart)


def _create_or_raise(name: str, config: dict[str, Any], autostart: bool) -> dict[str, Any]:
    """Shared by `create_connection` and `import_connection`: both end up
    calling `runtime.create()` with a core-shaped config dict and need the
    same core-error -> HTTP-status mapping."""
    try:
        record = get_runtime().create(name, config, autostart=autostart)
    except ValueError as exc:
        # ConnectionConfig.from_json / install-time validation.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ModuleNotFoundError as exc:
        # A `Structures` entry that names a module Python cannot find. The
        # whole message, not just `exc.name`: core says which config entry
        # failed and what to write instead, and a bare dotted package name
        # ("core.IRS.Structures.tiful") sent the last report of this on a long
        # detour -- it names neither the entry the user typed nor the fix.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ImportError as exc:
        # A structures file that exists but blew up while executing. It is
        # arbitrary user code -- and since it can be picked from a file dialog,
        # often code nothing has imported before -- so the reason belongs in
        # the modal, not in a 500 the user cannot see.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except OSError as exc:
        # autostart=True and the port is taken / address unusable.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"could not open the connection: {exc}",
        ) from exc
    return record.as_dict()


@router.get("/{connection_name}")
def get_connection(connection_name: str) -> dict[str, Any]:
    return _record(connection_name).as_dict()


@router.put("/{connection_name}")
def update_connection(connection_name: str, request: ConnectionUpdate) -> dict[str, Any]:
    """'Edit'. Rebuilds rather than mutates: `ConnectionConfig` is
    `frozen=True, slots=True` and a live `Connection` caches state derived from
    it, so in-place edits would desync those caches. Running state is
    preserved across the rebuild."""
    _record(connection_name)
    try:
        record = get_runtime().replace(connection_name, request.name, request.to_core_config())
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ModuleNotFoundError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"structures module not found: {exc.name}",
        ) from exc
    except ImportError as exc:
        # Same reasoning as create: a structures file is user code and its
        # failure belongs in the modal (see create_connection).
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"could not reopen the connection: {exc}",
        ) from exc
    return record.as_dict()


@router.delete("/{connection_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(connection_name: str) -> None:
    _record(connection_name)
    get_runtime().delete(connection_name)


@router.post("/{connection_name}/start")
def start_connection(connection_name: str) -> dict[str, Any]:
    _record(connection_name)
    try:
        return get_runtime().start(connection_name).as_dict()
    except OSError as exc:
        # The peer is not reachable (server not up, port taken, host down).
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Core refusing a route registration, or any other invariant it
        # enforces. Reported rather than left as a bare 500, so the toast says
        # what core actually objected to instead of "Internal Server Error".
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{connection_name}/stop")
def stop_connection(connection_name: str) -> dict[str, Any]:
    _record(connection_name)
    return get_runtime().stop(connection_name).as_dict()


def _record(connection_name: str):
    try:
        return get_runtime().get(connection_name)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
