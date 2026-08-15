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
    build_payload,
    get_runtime,
    list_messages,
    message_schema,
)

router = APIRouter(prefix="/api/connections/{connection_id}", tags=["messages"])

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
def connection_messages(connection_id: str, unit_name: str | None = None) -> list[dict[str, Any]]:
    """Every message this connection is permitted to SEND to `unit_name`.

    Queries with OUR unit code, because that is the code `irs_to_bytes` is
    called with on the way out (`Connection._encode` passes
    `self._own_unit_code`) -- but scoped to the DESTINATION's structures,
    because our own code is identical for every peer and therefore cannot say
    which link's layouts an opcode belongs to. Omitting `unit_name` lists every
    peer's messages, each row tagged with its namespace.
    """
    record = _record(connection_id)
    structures = record.structures_for(unit_name) if unit_name else None
    return list_messages(record.own_unit_code, structures)


@router.get("/messages/{op_code}/schema")
def message_form_schema(connection_id: str, op_code: int,
                        unit_name: str | None = None) -> dict[str, Any]:
    """The recursive form schema the Inspector renders, for one destination."""
    record = _record(connection_id)
    structures = record.structures_for(unit_name) if unit_name else None
    try:
        return message_schema(record.own_unit_code, op_code, structures)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IRSAmbiguousError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/send")
def send_message(connection_id: str, request: SendMessageRequest) -> dict[str, Any]:
    """Normalise the form payload, then send it.

    `build_payload` is what makes the Inspector's rules real: absent fields
    become 0 (otherwise IRS raises a bare `AttributeError` from inside
    `to_bytes`) and every counted array's length field is reconciled with its
    list (otherwise the receiver silently mis-parses).
    """
    record = _record(connection_id)
    try:
        # Scoped by destination: the same opcode may mean different layouts on
        # two links, and only the peer says which.
        schema = message_schema(record.own_unit_code, request.op_code,
                                record.structures_for(request.unit_name))
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IRSAmbiguousError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    payload = build_payload(schema, request.payload)
    try:
        return get_runtime().send(connection_id, request.unit_name, request.op_code, payload)
    except ValueError as exc:
        # Unknown unit name for this connection.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ConnectionError as exc:
        # No live peer on that unit yet (e.g. UDP server before first inbound).
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/logs/{direction}")
def logs(connection_id: str, direction: str) -> list[dict[str, Any]]:
    """History for the orange (sent) / brown (received) panels. The live feed
    arrives over the WebSocket; this backfills on (re)connect."""
    if direction not in ("sent", "received"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="direction must be sent|received")
    _record(connection_id)
    return get_runtime().logs(connection_id, direction)


def _record(connection_id: str):
    try:
        return get_runtime().get(connection_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
