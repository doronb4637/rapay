"""
Read-only view over a link's IRS interface -- what the Messages panel queries.

There is no global registry: every core `Connection` holds one `Interface` per
link (`Connection.interface_for(unit_name)`), built from that link's own
`Structures` files. Everything here takes that `Interface`, so GSim always shows
exactly the layouts core will parse and send with on that link.

A log entry remembers only the module its message class came from, so
`interface_of_module` rebuilds an `Interface` from that one module for the
Inspector.

Nothing here mutates an interface; GSim only reads it.
"""
from __future__ import annotations

import sys
from typing import Any

from . import bootstrap  # noqa: F401

from core.IRS.irs_parser import (
    Interface,
    IRSAmbiguousError,
    IRSDataError,
    IRSNotFoundError,
)

#: Re-exported so `gsim.api` can map these to HTTP errors without importing
#: core -- nothing above this package is allowed to reach into IRS directly.
__all__ = [
    "Interface", "IRSAmbiguousError", "IRSDataError", "IRSNotFoundError",
    "list_messages", "message_schema", "resolve_route", "known_unit_codes",
    "interface_of_module", "field_targets",
]

from .schema import describe_message, field_targets  # noqa: F401


def interface_of_module(namespace: str) -> Interface:
    """The interface of one already-loaded structures module.

    Raises `KeyError` when no connection has loaded it (yet).
    """
    module = sys.modules.get(namespace)
    if module is None:
        raise KeyError(f"structures module {namespace!r} is not loaded by any connection")
    return Interface.from_module(module)


def list_messages(unit_code: int, interface: Interface) -> list[dict[str, Any]]:
    """Every message `unit_code` sends on this link, as summaries (no field
    schema -- the Messages panel only needs names and opcodes). `namespace` is
    the module the class was defined in, which is what a log entry carries."""
    return [
        {
            "unit_code": unit_code,
            "op_code": op_code,
            "op_code_hex": f"0x{op_code:04X}",
            "name": message_class.__name__,
            "field_count": len(message_class._fields_),
            "namespace": message_class.__module__,
        }
        for op_code, message_class in interface.routes(unit_code)
    ]


def resolve_route(unit_code: int, op_code: int, interface: Interface) -> tuple[type, str]:
    """`(message class, namespace)` for one route -- everything `message_schema`
    does before it describes a single field.

    Split out because the SEND path needs only this. The recursive field walk
    exists to tell the Inspector which widgets to draw; building the whole thing
    to read two strings off it was the bulk of the work on every send and every
    behaviour tick.

    Raises `KeyError` with a UI-presentable message when the link defines no
    such route.
    """
    try:
        message_class = interface.message_class(unit_code, op_code)
    except IRSNotFoundError as exc:
        raise KeyError(str(exc)) from exc
    return message_class, message_class.__module__


def message_schema(unit_code: int, op_code: int, interface: Interface) -> dict[str, Any]:
    """The full recursive form schema for one message -- what the Inspector
    renders its form from. Same error contract as `resolve_route`."""
    message_class, namespace = resolve_route(unit_code, op_code, interface)
    schema = describe_message(message_class, unit_code, op_code)
    schema["namespace"] = namespace
    return schema


def known_unit_codes(interface: Interface) -> list[int]:
    return interface.unit_codes()
