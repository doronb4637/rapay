"""
Behaviours: what this simulated unit sends, and what makes it send.

Same threading rule as the rest of the API (`def`, never `async def`): although
these handlers do not themselves block on core, `PUT` normalises a payload and
resolves up to two schemas, and the engine they hand it to drives
`runtime.sender()` on its own worker threads.

`PUT` is an upsert keyed by `(connection, unit_name, op_code, trigger,
trigger_unit_name, trigger_op_code)` rather than a `POST` that mints a new
resource each call: the outbound route plus what fires it identifies a rule (see
`core_gateway/behaviours.py`).

**This is the layer that knows what a condition or a mapping may address**, and
it is the only one. `behaviours.py` sees paths and payloads; `schema.py` knows
which fields exist; this joins the two -- and it is the only place that can,
because a reactive behaviour spans TWO messages and only here are both
resolvable: the condition and every mapping source belong to the INCOMING
message (the peer's unit code), every mapping target to the OUTGOING one (ours).
Getting that backwards is the mistake this module exists to make impossible.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from gsim.api.models import BehaviourRequest
from gsim.core_gateway import (
    EQUALITY_OPERATORS,
    IRSAmbiguousError,
    field_targets,
    get_runtime,
    message_schema,
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
    """Create or replace one behaviour.

    The payload is normalised HERE, once, by the same `prepare_message` the manual
    send path uses -- letting IRS's `fill()` supply every absent field and
    reconciling counted-array lengths. Doing it at configure time rather than per
    tick means a payload that could never encode fails in this request, where the
    modal can show why, instead of logging the identical error forever on a worker
    thread. What is stored is the message's `to_dict()` form, which the engine's worker
    hands to `runtime.sender()` once, when it starts; a tick fires the message
    built from it rather than rebuilding it.
    """
    runtime = get_runtime()
    try:
        record = runtime.get(connection_name)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    peers = record.peers()
    if request.unit_name not in peers:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"unknown connected unit {request.unit_name!r}; "
                   f"known: {sorted(peers)}",
        )

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
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    _check_trigger_fields(record, request, outgoing_name=prepared.name)

    try:
        behaviour = runtime.behaviours.set(
            connection_name=connection_name,
            unit_name=request.unit_name,
            op_code=request.op_code,
            payload=prepared.payload,
            kind=request.kind,
            trigger=request.trigger,
            mode=request.mode,
            interval=request.interval,
            message_name=prepared.name,
            enabled=request.enabled,
            trigger_unit_name=request.trigger_unit_name,
            trigger_op_code=request.trigger_op_code,
            condition=None if request.condition is None else request.condition.model_dump(),
            delay_ms=request.delay_ms,
            mappings=[{"from": m.source, "to": m.target} for m in request.mappings],
        )
    except ValueError as exc:
        # Unknown trigger/mode, or an interval below the engine's floor.
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


# -- trigger validation --------------------------------------------------
def _check_trigger_fields(record: Any, request: BehaviourRequest,
                          outgoing_name: str) -> None:
    """Resolve the incoming message and check every path the rule names.

    Only `on_received` reaches the interesting half: it is the one trigger that
    has an incoming message at all, and therefore the only one whose condition
    and mappings have a left-hand side.
    """
    if request.trigger != "on_received":
        return

    # The INCOMING side is keyed by the SENDER's unit code -- a received message
    # is decoded under `parse_irs(their_code, ...)`, so asking under our own
    # would find the wrong layout or none. `trigger_unit_name` defaults to the
    # unit we answer, which is the ordinary request/response case.
    peers = record.peers()
    source_unit = request.trigger_unit_name or request.unit_name
    if source_unit not in peers:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"unknown trigger unit {source_unit!r}; known: {sorted(peers)}",
        )
    try:
        incoming = message_schema(peers[source_unit], request.trigger_op_code,
                                  record.structures_for(source_unit))
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"{source_unit} sends no message with that opCode: {exc}") from exc
    except IRSAmbiguousError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    outgoing = message_schema(record.own_unit_code, request.op_code,
                              record.structures_for(request.unit_name))
    incoming_targets = {t["path"]: t for t in field_targets(incoming)}
    outgoing_targets = {t["path"]: t for t in field_targets(outgoing)}

    if request.condition is not None:
        target = _addressable(request.condition.path, incoming_targets, incoming["name"])
        if target["kind"] == "enum":
            # An enum decodes to its member NAME, so ordering it would compare
            # strings alphabetically -- not what anyone means by `<`.
            if request.condition.op not in EQUALITY_OPERATORS:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"{request.condition.path!r} is an enum; use "
                           f"{' or '.join(EQUALITY_OPERATORS)}, not {request.condition.op!r}.",
                )
            names = [option["name"] for option in target.get("options", [])]
            if request.condition.value is not None and request.condition.value not in names:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"{request.condition.value!r} is not a member of "
                           f"{target.get('enum', 'that enum')}; known: {names}",
                )
        elif not isinstance(request.condition.value, (int, float)):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{request.condition.path!r} is a number; "
                       f"{request.condition.value!r} is not.",
            )

    for mapping in request.mappings:
        source = _addressable(mapping.source, incoming_targets, incoming["name"])
        target = _addressable(mapping.target, outgoing_targets, outgoing_name)
        # A value copied between two different kinds cannot arrive intact: an
        # enum travels as a member NAME and a scalar as a number, so either
        # direction produces a payload IRS refuses -- caught here rather than as
        # a send failure repeated forever on a worker thread.
        if source["kind"] != target["kind"]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"cannot copy {mapping.source!r} ({source['kind']}) into "
                       f"{mapping.target!r} ({target['kind']}) -- an enum travels as a "
                       f"member name and a number as a number.",
            )
        if source["kind"] == "enum" and source.get("enum") != target.get("enum"):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"cannot copy {mapping.source!r} ({source.get('enum')}) into "
                       f"{mapping.target!r} ({target.get('enum')}) -- the member names "
                       f"of one enum mean nothing to another.",
            )


def _addressable(path: str, targets: dict[str, dict[str, Any]],
                 message_name: str) -> dict[str, Any]:
    """One field a condition or a mapping may name, or a 422 saying why not.

    Three distinct failures kept distinct, because they call for three different
    fixes -- the same treatment `routes/filters.py` gives them, and the
    array-interior case is again the likeliest mistake by a distance.
    """
    target = targets.get(path)
    if target is None:
        array = _enclosing_array(path, targets)
        if array is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{path!r} is inside {array!r}, a repeating array. A single path "
                       f"names one value, and this one names one per element.",
            )
        usable = sorted(name for name, entry in targets.items() if entry["rule_ok"])
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{message_name} has no field {path!r}; known: {usable}",
        )
    if not target["rule_ok"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{path!r} is a {target['kind']}, which holds no single value. "
                   f"Name one of its fields instead.",
        )
    return target


def _enclosing_array(path: str, targets: dict[str, dict[str, Any]]) -> str | None:
    """The array this path reaches into, if it reaches into one."""
    parts = path.split(".")
    for depth in range(1, len(parts)):
        prefix = ".".join(parts[:depth])
        target = targets.get(prefix)
        if target is not None and target["kind"] == "array":
            return prefix
    return None
