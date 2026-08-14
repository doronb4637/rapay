"""
Read-only view over `IRS.REGISTRY` -- the GLOBAL REGISTRY the Messages panel
queries with a connection's unit code.

`IRS.REGISTRY.MESSAGE_REGISTRY` is a module-level dict populated as a side
effect of *importing* a structures module (`register_message()` runs at module
scope). `ConnectionManager._import_config_libs` does that import when a
connection is created, from the config's `Structures` key -- so by the time a
connection exists, its layouts are already registered.

Nothing here mutates the registry; GSim only reads it.
"""
from __future__ import annotations

from typing import Any

from . import bootstrap  # noqa: F401

from IRS.REGISTRY import MESSAGE_REGISTRY, PAIR_REGISTRY

from .schema import describe_message


def _resolve_unit(unit_code: int) -> dict[int, type] | None:
    """`MESSAGE_REGISTRY[unit]`, falling back through `PAIR_REGISTRY` exactly
    the way `IRS.irs_parser._get_message_class` does -- so the UI lists the same
    messages the parser would actually accept for that unit."""
    messages = MESSAGE_REGISTRY.get(unit_code)
    if messages is None:
        messages = MESSAGE_REGISTRY.get(PAIR_REGISTRY.get(unit_code))
    return messages


def list_messages(unit_code: int) -> list[dict[str, Any]]:
    """Every message `unit_code` is permitted to send, as summaries (no field
    schema -- the Messages panel only needs names and opcodes)."""
    messages = _resolve_unit(unit_code)
    if not messages:
        return []
    return sorted(
        (
            {
                "unit_code": unit_code,
                "op_code": op_code,
                "op_code_hex": f"0x{op_code:04X}",
                "name": message_class.__name__,
                "field_count": len(message_class._fields_),
            }
            for op_code, message_class in messages.items()
        ),
        key=lambda entry: entry["op_code"],
    )


def message_schema(unit_code: int, op_code: int) -> dict[str, Any]:
    """The full recursive form schema for one message. Raises `KeyError` with a
    UI-presentable message when the route is not registered."""
    messages = _resolve_unit(unit_code)
    if not messages or op_code not in messages:
        raise KeyError(
            f"unit {unit_code} has no message registered for opCode "
            f"0x{op_code:04X}; known: {sorted(messages or {})}"
        )
    return describe_message(messages[op_code], unit_code, op_code)


def known_unit_codes() -> list[int]:
    return sorted(MESSAGE_REGISTRY)
