"""
Read-only view over the IRS registry -- what the Messages panel queries for a
link.

Layouts are registered as a side effect of *importing* a structures module
(`register_message()` runs at module scope), and `ConnectionManager` does that
import when a connection is created, from the config's `Structures` key -- so by
the time a connection exists, its layouts are already registered.

Everything here goes through `IRS.irs_parser` rather than reading the registry
dicts directly. That matters: a structures file describes ONE link, so which
layout answers a route depends on which module the link declared, and the
pair-alias fallback has to be applied per module. Duplicating that lookup order
here is exactly how GSim would end up showing a different message than core
would actually parse.

`namespaces` is the resolved structures module list for a link (core's
`ConnectionConfig.structures_for(unit_name)`). Omitting it searches every
registered module -- correct only while no two of them define the same route,
which is what `IRSAmbiguousError` is there to catch.

Nothing here mutates the registry; GSim only reads it.
"""
from __future__ import annotations

from typing import Any, Sequence

from . import bootstrap  # noqa: F401

from core.IRS.irs_parser import (
    IRSAmbiguousError,
    IRSDataError,
    IRSNotFoundError,
    get_message_class,
    known_unit_codes as _known_unit_codes,
    list_routes,
)

#: Re-exported so `gsim.api` can map these to HTTP errors without importing
#: core -- nothing above this package is allowed to reach into IRS directly.
__all__ = [
    "IRSAmbiguousError", "IRSDataError", "IRSNotFoundError",
    "list_messages", "message_schema", "resolve_route", "known_unit_codes",
    "filter_targets",
]

from .schema import describe_message, filter_targets  # noqa: F401


def list_messages(unit_code: int, namespaces: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Every message `unit_code` is permitted to send, as summaries (no field
    schema -- the Messages panel only needs names and opcodes).

    Each row carries the `namespace` it came from, so two modules defining one
    opcode differently show up as two honest rows instead of one arbitrary
    winner.
    """
    return [
        {
            "unit_code": unit_code,
            "op_code": op_code,
            "op_code_hex": f"0x{op_code:04X}",
            "name": message_class.__name__,
            "field_count": len(message_class._fields_),
            "namespace": namespace,
        }
        for op_code, message_class, namespace in list_routes(unit_code, namespaces)
    ]


def resolve_route(unit_code: int, op_code: int,
                  namespaces: Sequence[str] | None = None) -> tuple[type, str | None]:
    """`(message class, namespace)` for one route -- everything `message_schema`
    does before it describes a single field.

    Split out because the SEND path needs only this. The recursive field walk
    exists to tell the Inspector which widgets to draw; building the whole thing
    to read two strings off it was the bulk of the work on every send and every
    behaviour tick.

    Raises `KeyError` with a UI-presentable message when the route is not
    registered. `IRSAmbiguousError` is deliberately NOT converted: "this route
    means two different things and you did not say which" is a 409, not a 404,
    and the route layer needs to tell them apart.
    """
    try:
        # Resolution, not listing: this goes through the parser so an ambiguous
        # route raises IRSAmbiguousError here rather than quietly answering with
        # whichever module happened to be found first.
        message_class = get_message_class(unit_code, op_code, namespaces)
    except IRSNotFoundError as exc:
        raise KeyError(str(exc)) from exc
    if message_class is None:
        known = sorted({route[0] for route in list_routes(unit_code, namespaces)})
        raise KeyError(
            f"unit {unit_code} has no message registered for opCode "
            f"0x{op_code:04X}; known: {known}"
        )
    namespace = next(
        (name
         for route_op_code, _message_cls, name in list_routes(unit_code, namespaces)
         if route_op_code == op_code),
        None,
    )
    return message_class, namespace


def message_schema(unit_code: int, op_code: int,
                   namespaces: Sequence[str] | None = None) -> dict[str, Any]:
    """The full recursive form schema for one message -- what the Inspector
    renders its form from. Same error contract as `resolve_route`."""
    message_class, namespace = resolve_route(unit_code, op_code, namespaces)
    schema = describe_message(message_class, unit_code, op_code)
    schema["namespace"] = namespace
    return schema


def known_unit_codes(namespaces: Sequence[str] | None = None) -> list[int]:
    return _known_unit_codes(namespaces)
