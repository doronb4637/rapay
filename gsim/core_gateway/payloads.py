"""
A form payload -> a real IRS message, ready to encode.

**IRS's own `fill()` owns absence.** Every field nobody set gets its safe default at
any depth, recursively, from the field objects themselves -- GSim used to carry a
second copy of that table (`_default_for`, driven off the JSON form schema) and no
longer does.

What is left here is only what is genuinely GSim's, because IRS neither can nor
should decide it:

1. **Blanks.** An untouched input submits `""`, which is not "unset" to anything in
   IRS -- it reaches `struct.pack` and raises `required argument is not an integer`.
   Dropping it lets `fill()` treat the field as absent, which is what the user meant.

2. **Fixed-array size.** A `[Data, 9]` field given 3 items is a short frame, and core
   now rejects it outright (`ArrayField.to_bytes`). GSim pads and truncates instead:
   a form should be forgiving about how many rows you happened to fill in.

3. **Counted-array lengths.** `ArrayField.to_bytes` never writes the count and
   `from_bytes` reads exactly `getattr(instance, count_field)` items, so a
   disagreement raises nothing on send -- the receiver simply mis-parses. The count
   is therefore DERIVED from the list here, never taken from the form. It cannot be
   fixed in IRS: `to_bytes(writer, value)` has no access to the sibling that holds it.

An enum with no `0` member is left explicitly unset (`None`) rather than guessed at;
`fill()` produces that and `EnumField.to_bytes` writes it as `0`.
"""
from __future__ import annotations

from typing import Any, NamedTuple, Sequence

from . import bootstrap  # noqa: F401  -- must precede every `core` import

from core.IRS.bitfields import BitField
from core.IRS.core import ArrayField, Structure

from .registry import resolve_route


class Prepared(NamedTuple):
    """Everything the send path and the behaviour engine need from one route."""
    message: Any                 # the built IRS message, ready for send_message
    name: str
    namespace: str | None
    payload: dict[str, Any]      # its canonical `to_dict()` form, for the log


def prepare_message(unit_code: int, op_code: int, namespaces: Sequence[str] | None,
                    raw: dict[str, Any] | None) -> Prepared:
    """Resolve the route once, then build and complete the message.

    Raises `KeyError` / `IRSAmbiguousError` for an unresolvable route (see
    `registry.resolve_route`) and `ValueError` for a value IRS refuses, such as a
    number that names no member of an enum.
    """
    message_class, namespace = resolve_route(unit_code, op_code, namespaces)
    message = message_class.from_dict(_prepare(message_class._fields_, raw)).fill()
    return Prepared(message, message_class.__name__, namespace, message.to_dict())


def _blank(value: Any) -> Any:
    """`None` for anything the form left empty, so `fill()` owns it."""
    return None if value is None or value == "" else value


def _prepare(fields: tuple, data: dict[str, Any] | None) -> dict[str, Any]:
    """Walks `_fields_` -- the IRS field objects, not the JSON schema."""
    data = data or {}
    out: dict[str, Any] = {}

    for field in fields:
        value = _blank(data.get(field._name))
        if value is None:
            continue                       # absent -> fill() owns it
        out[field._name] = _prepare_value(field, value)

    # Second pass: a counted array's length lives in a SIBLING field, so it can only
    # be reconciled once every sibling has been walked. Always derived from the list
    # -- an array the form omitted counts 0, whatever the form said the count was.
    for field in fields:
        if isinstance(field, ArrayField) and isinstance(field.length, str):
            out[field.length] = len(out.get(field._name, ()))

    return out


def _prepare_value(field: Any, value: Any) -> Any:
    """One field's value, which may itself be a struct/bitfield/array."""
    if isinstance(field, BitField):
        source = value if isinstance(value, dict) else {}
        # Absent and blank bits are dropped: `BitField.from_dict` starts from a
        # zeroed instance and assigns only what it is given.
        return {bit: val for bit, val in source.items() if _blank(val) is not None}

    if isinstance(field, Structure):
        return _prepare(type(field)._fields_, value if isinstance(value, dict) else {})

    if isinstance(field, ArrayField):
        items = list(value) if isinstance(value, list) else []
        if isinstance(field.length, int):
            items = items[:field.length]
            items += [None] * (field.length - len(items))
        prepared = []
        for item in items:
            # A blank element needs the filler too, not just a missing one: an
            # element is a value in a list, so `fill()` has no absent slot to see.
            item = _blank(item)
            prepared.append(_prepare_value(
                field.baseType, _filler(field.baseType) if item is None else item))
        return prepared

    return value


def _filler(base_type: Any) -> Any:
    """One element for a fixed-length array the form left short."""
    if isinstance(base_type, (Structure, BitField)):
        # An empty dict builds an empty instance, which `fill()` then completes --
        # rather than duplicating IRS's defaults for the element's own fields here.
        return {}
    return base_type.fill()
