"""
The GSim <-> core boundary.

**This package is the only place in GSim that imports `core`.** Everything
above it (`gsim.api`, `gsim.web`) talks to these modules and never to
`connections`/`IRS`/`tools` directly, so the whole dependency on core is
swappable and greppable from one directory.

    bootstrap.py   puts <repo-root> on sys.path (must import first)
    schema.py      IRS message class -> JSON form schema
    registry.py    namespace-scoped read-only view of the IRS registry
    payloads.py    form payload -> a built IRS message, ready to encode
    dds.py         DDS topics as messages: listing, form schema, samples
    routekey.py    a route key is an IRS opcode (int) or a DDS topic name (str)
    filters.py     which RECEIVED messages are worth logging at all
    runtime.py     GSim's connection registry, logs, and thread bridge
"""
from .bootstrap import CORE_ROOT, ensure_core_importable
from .behaviours import (
    LEGACY_KINDS as BEHAVIOUR_LEGACY_KINDS,
    MAX_DELAY_MS as BEHAVIOUR_MAX_DELAY_MS,
    MODES as BEHAVIOUR_MODES,
    TRIGGERS as BEHAVIOUR_TRIGGERS,
)
from .dds import dds_defaults, dds_units
from .dds import describe_topic as _describe_topic, topic_class as _topic_class
from .filters import ACTIONS, EQUALITY_OPERATORS, MODES, OPERATORS, FilterSet
from .payloads import Prepared, prepare_message
from .registry import (
    IRSAmbiguousError, IRSDataError, IRSNotFoundError,
    field_targets, known_unit_codes, list_messages, message_schema, resolve_route,
)
from .routekey import route_label
from .runtime import GSimRuntime, get_runtime


def entry_schema(unit_code: int, op_code: str, namespace: str | None) -> dict:
    """Form schema for a LOG ENTRY's message, from what the entry carries.

    `op_code` arrives as text (a URL path segment). A DDS entry's `namespace`
    is its Interface and its route key is a topic name; an IRS entry's
    namespace is its structures module and its key is an opcode.
    """
    try:
        irs_op_code = int(op_code)
    except ValueError:
        if not namespace:
            raise KeyError(f"topic {op_code!r} needs the DDS Interface it came from") from None
        return _describe_topic(_topic_class(namespace, op_code), unit_code, namespace)
    return message_schema(unit_code, irs_op_code, [namespace] if namespace else None)


__all__ = [
    "CORE_ROOT", "ensure_core_importable",
    "ACTIONS", "EQUALITY_OPERATORS", "MODES", "OPERATORS", "FilterSet",
    "BEHAVIOUR_LEGACY_KINDS", "BEHAVIOUR_MAX_DELAY_MS", "BEHAVIOUR_MODES",
    "BEHAVIOUR_TRIGGERS",
    "Prepared", "prepare_message",
    "IRSAmbiguousError", "IRSDataError", "IRSNotFoundError",
    "field_targets", "known_unit_codes", "list_messages", "message_schema",
    "resolve_route", "entry_schema", "route_label", "dds_defaults", "dds_units",
    "GSimRuntime", "get_runtime",
]
