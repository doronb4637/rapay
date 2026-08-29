"""
Messages directory (gray box), the Inspector's form schema (red box), and send.

Same threading rule as connections.py: `def`, never `async def`, because
`send_message` blocks on core's loop.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from gsim.api.models import SendMessageRequest
from gsim.core_gateway import (
    IRSAmbiguousError,
    IRSDataError,
    IRSNotFoundError,
    get_runtime,
    list_messages,
    message_schema,
)

router = APIRouter(prefix="/api/connections/{connection_name}", tags=["messages"])

#: Registry lookups that are not scoped to one connection.
registry_router = APIRouter(prefix="/api", tags=["registry"])


@registry_router.get("/logs/{direction}")
def all_logs(direction: str) -> list[dict[str, Any]]:
    """Every connection's log for one direction.

    The console is process-wide, not per-connection: a send is logged against
    the SENDER and the matching receive against the RECIPIENT, so filtering to
    one selected connection can only ever show half of any exchange.
    """
    if direction not in ("sent", "received"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="direction must be sent|received")
    return get_runtime().all_logs(direction)


@registry_router.delete("/logs/{direction}")
def clear_all_logs(direction: str) -> dict[str, int]:
    """Empty one direction's history for every connection.

    Server-side, not a client-side view filter: these buffers are what
    `GET /api/logs/{direction}` backfills from, so clearing only the browser's
    copy would put everything back on the next refresh.
    """
    if direction not in ("sent", "received"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="direction must be sent|received")
    return {"cleared": get_runtime().clear_logs(direction)}


@registry_router.get("/schema/{unit_code}/{op_code}")
def schema_by_unit(unit_code: int, op_code: int, namespace: str | None = None) -> dict[str, Any]:
    """Form schema for an explicit (unitCode, opCode).

    The Inspector needs this for RECEIVED messages: a message is decoded with
    the SENDER's unit code (`parse_irs(their_code, ...)`), so rendering it
    against our own code would find the wrong layout -- or none at all.

    `namespace` names the structures module the entry was decoded with. It is
    required whenever two modules define the same route, which is why every log
    entry carries it: (unitCode, opCode) alone stopped being a unique key the
    moment structures became per-link.
    """
    try:
        return message_schema(unit_code, op_code, [namespace] if namespace else None)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IRSAmbiguousError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/messages")
def connection_messages(connection_name: str, unit_name: str | None = None) -> list[dict[str, Any]]:
    """Every message this connection is permitted to SEND to `unit_name`.

    Queries with OUR unit code, because that is the code `irs_to_bytes` is
    called with on the way out (`Connection._encode` passes
    `self._own_unit_code`) -- but scoped to the DESTINATION's structures,
    because our own code is identical for every peer and therefore cannot say
    which link's layouts an opcode belongs to. Omitting `unit_name` lists every
    peer's messages, each row tagged with its namespace.
    """
    record = _record(connection_name)
    structures = record.structures_for(unit_name) if unit_name else None
    return list_messages(record.own_unit_code, structures)


@router.get("/messages/{op_code}/schema")
def message_form_schema(connection_name: str, op_code: int,
                        unit_name: str | None = None) -> dict[str, Any]:
    """The recursive form schema the Inspector renders, for one destination."""
    record = _record(connection_name)
    structures = record.structures_for(unit_name) if unit_name else None
    try:
        return message_schema(record.own_unit_code, op_code, structures)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IRSAmbiguousError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/send")
def send_message(connection_name: str, request: SendMessageRequest) -> dict[str, Any]:
    """Send one composed message.

    Normalisation lives in `runtime.send` (via `prepare_message`) rather than
    here: it resolves the route ONCE and builds the IRS message the encoder
    actually needs, so this route is only the HTTP error mapping.
    """
    _record(connection_name)
    try:
        return get_runtime().send(connection_name, request.unit_name, request.op_code,
                                  request.payload)
    except KeyError as exc:
        # No layout registered for this route -- the request named an opcode this
        # link's structures do not define.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IRSAmbiguousError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ConnectionError as exc:
        # No live peer on that unit yet (e.g. UDP server before first inbound).
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (IRSDataError, IRSNotFoundError) as exc:
        # The payload core actually got couldn't be encoded -- reported rather
        # than left as a bare 500, so the toast says what core objected to.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except RuntimeError as exc:
        # E.g. a receive_only unit (UDP client side never bound to send).
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        # An unknown unit name for this connection, or a value IRS refuses while
        # the message is being built (a number naming no member of an enum).
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/logs/{direction}")
def logs(connection_name: str, direction: str) -> list[dict[str, Any]]:
    """History for the orange (sent) / brown (received) panels. The live feed
    arrives over the WebSocket; this backfills on (re)connect."""
    if direction not in ("sent", "received"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="direction must be sent|received")
    _record(connection_name)
    return get_runtime().logs(connection_name, direction)


def _record(connection_name: str):
    try:
        return get_runtime().get(connection_name)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
