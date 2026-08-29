"""
Received-message filters: list / upsert / arm / disarm / delete.

Same threading rule as the rest of the API (`def`, never `async def`): these
handlers do not block on core themselves, but `PUT` resolves a schema, and the
`FilterSet` they hand it to is read from core's executor threads.

`PUT` is an upsert keyed by `(connection, unit_name, op_code)` rather than a
`POST` minting a new resource per call, for the same reason behaviours are: one
inbound route can only have one filter, because two would contradict rather
than compose (see `core_gateway/filters.py`).

**This is the layer that knows what a rule may address**, and it is the only
one. `filters.py` sees paths and payloads and nothing else; `schema.py` knows
which fields exist; this joins the two, so a rule naming a field that does not
exist -- or one buried inside an array, which no single path can name -- fails
in the request, where the modal shows why, instead of silently never matching.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from gsim.api.models import FilterRequest
from gsim.core_gateway import (
    EQUALITY_OPERATORS,
    IRSAmbiguousError,
    filter_targets,
    get_runtime,
    message_schema,
)

router = APIRouter(prefix="/api", tags=["filters"])


@router.get("/filters")
def list_filters() -> list[dict[str, Any]]:
    """Every configured filter, across every connection.

    Process-wide for the same reason the console and the behaviours list are: a
    filter keeps dropping messages while you look at a different connection, and
    a view that hid it would be the one place the UI lies about what it is
    doing.
    """
    return get_runtime().filters.list()


@router.put("/connections/{connection_name}/filters")
def set_filter(connection_name: str, request: FilterRequest) -> dict[str, Any]:
    """Create or replace the filter on one inbound route."""
    runtime = get_runtime()
    record = _record(connection_name)

    peers = record.peers()
    if request.unit_name not in peers:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"unknown connected unit {request.unit_name!r}; known: {sorted(peers)}",
        )
    # Queried with the PEER's unit code, not ours: a received message is decoded
    # under the sender's code (`parse_irs(their_code, ...)`), so asking under our
    # own would find the wrong layout or none at all. Scoped to that peer's
    # structures for the usual reason -- one opcode can mean two layouts on two
    # links of the same connection.
    peer_code = peers[request.unit_name]
    try:
        schema = message_schema(peer_code, request.op_code, record.structures_for(request.unit_name))
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IRSAmbiguousError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    targets = {target["path"]: target for target in filter_targets(schema)}
    for rule in request.rules:
        _check_rule(rule, targets, schema["name"])
    if request.change_field and request.change_field not in targets:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_unknown_field(request.change_field, schema["name"], targets, rule_only=False),
        )

    try:
        message_filter = runtime.filters.set(
            connection_name=connection_name,
            unit_name=request.unit_name,
            unit_code=peer_code,
            op_code=request.op_code,
            message_name=schema["name"],
            namespace=schema.get("namespace"),
            mode=request.mode,
            change_field=request.change_field,
            rules=[rule.model_dump() for rule in request.rules],
            armed=request.armed,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return message_filter.as_dict()


@router.post("/filters/{filter_id}/arm")
def arm_filter(filter_id: str) -> dict[str, Any]:
    """Start deciding again, from a clean record -- counters and the change
    baseline both reset, because the peer's state is unknown after however long
    this spent disarmed."""
    return _set_armed(filter_id, True)


@router.post("/filters/{filter_id}/disarm")
def disarm_filter(filter_id: str) -> dict[str, Any]:
    """Stop deciding but keep the configuration, so it can be re-armed without
    re-entering its rules."""
    return _set_armed(filter_id, False)


@router.post("/filters/disarm-all")
def disarm_all_filters() -> list[dict[str, Any]]:
    """Every filter stops deciding at once. This is what the console's 'Show
    all' does -- the fastest action in the dialog is undoing all of it, which is
    what makes dropping messages server-side an acceptable trade."""
    runtime = get_runtime()
    runtime.filters.disarm_all()
    return runtime.filters.list()


@router.delete("/filters/{filter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_filter(filter_id: str) -> None:
    try:
        get_runtime().filters.delete(filter_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# -- internals -----------------------------------------------------------
def _set_armed(filter_id: str, armed: bool) -> dict[str, Any]:
    try:
        return get_runtime().filters.set_armed(filter_id, armed).as_dict()
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _check_rule(rule: Any, targets: dict[str, dict[str, Any]], message_name: str) -> None:
    """Reject a rule this message cannot answer, naming the reason.

    Three distinct failures, kept distinct because they call for three different
    fixes: the field does not exist (typo), the field exists but nothing can be
    compared against it (an array or a struct), or the operator makes no sense
    for the field's type.
    """
    target = targets.get(rule.path)
    if target is None:
        # `Areas.fr` is the likeliest mistake by a distance -- the field really
        # exists, just once per element -- and it lands here rather than in the
        # branch below, because nothing inside an array is ever offered as a
        # target at all. Answering it with a bare "no such field" would send the
        # user hunting for a typo that is not there.
        array = _enclosing_array(rule.path, targets)
        if array is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{rule.path!r} is inside {array!r}, a repeating array. A rule "
                       f"compares one value, and this path names one per element -- "
                       f"watch {array!r} or the whole message for changes instead.",
            )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_unknown_field(rule.path, message_name, targets, rule_only=True),
        )
    if not target["rule_ok"]:
        kind = target["kind"]
        extra = (
            " Watch it for changes instead, which compares the whole list."
            if kind == "array" else
            " Match one of its fields instead."
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{rule.path!r} is a {kind}, which has no single value to compare.{extra}",
        )
    if target["kind"] == "enum":
        # An enum decodes to its member NAME, so ordering it would compare
        # strings alphabetically -- which is not what any user means by `<`.
        if rule.op not in EQUALITY_OPERATORS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{rule.path!r} is an enum; use {' or '.join(EQUALITY_OPERATORS)}, "
                       f"not {rule.op!r}.",
            )
        names = [option["name"] for option in target.get("options", [])]
        if rule.value is not None and rule.value not in names:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{rule.value!r} is not a member of {target.get('enum', 'that enum')}; "
                       f"known: {names}",
            )
    elif not isinstance(rule.value, (int, float)):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{rule.path!r} is a number; {rule.value!r} is not.",
        )


def _enclosing_array(path: str, targets: dict[str, dict[str, Any]]) -> str | None:
    """The array this path reaches into, if it reaches into one.

    Walks the prefixes rather than checking the parent alone, so a path several
    levels deep inside an array is still explained by the array that owns it.
    """
    parts = path.split(".")
    for depth in range(1, len(parts)):
        prefix = ".".join(parts[:depth])
        target = targets.get(prefix)
        if target is not None and target["kind"] == "array":
            return prefix
    return None


def _unknown_field(path: str, message_name: str, targets: dict[str, dict[str, Any]],
                   rule_only: bool) -> str:
    usable = sorted(
        name for name, target in targets.items() if target["rule_ok"] or not rule_only
    )
    return f"{message_name} has no field {path!r}; known: {usable}"


def _record(connection_name: str):
    try:
        return get_runtime().get(connection_name)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
