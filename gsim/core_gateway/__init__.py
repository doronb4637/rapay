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
    runtime.py     GSim's connection registry, logs, and thread bridge
"""
from .bootstrap import CORE_ROOT, ensure_core_importable
from .payloads import Prepared, prepare_message
from .registry import (
    IRSAmbiguousError, IRSDataError, IRSNotFoundError,
    known_unit_codes, list_messages, message_schema, resolve_route,
)
from .runtime import GSimRuntime, get_runtime

__all__ = [
    "CORE_ROOT", "ensure_core_importable",
    "Prepared", "prepare_message",
    "IRSAmbiguousError", "IRSDataError", "IRSNotFoundError",
    "known_unit_codes", "list_messages", "message_schema", "resolve_route",
    "GSimRuntime", "get_runtime",
]
