"""
IRS message class  ->  JSON schema the UI can render a form from.

This is pure introspection: it reads `Message._fields_` (built by
`IRS.core.MessageMeta` at class-definition time) and never mutates anything in
`core`. Every field kind the IRS metaclass can produce is covered:

    Field       -> {"kind": "scalar"}    an int/float, dtype named (UInt16, ...)
    EnumField   -> {"kind": "enum"}      renders as a <select>
    Structure   -> {"kind": "struct"}    nested fields, rendered recursively
    BitField    -> {"kind": "bitfield"}  packed bits; each bit-range is an input
    ArrayField  -> {"kind": "array"}     see `length` below

`ArrayField.length` is the load-bearing distinction for the UI:

    None    -> "dynamic"  consumes the rest of the buffer; grows by user action
    str     -> "counted"  its length lives in a SIBLING field of that name
    int     -> "fixed"    exactly N items, UI must not offer add/remove

The "counted" case needs care on the way out: `ArrayField.to_bytes` iterates the
list and never writes the count itself, while `from_bytes` reads exactly
`getattr(instance, length_field)` items. A count that disagrees with the list is
therefore silent wire corruption, not an error -- `payloads.py` keeps the two in
sync, which is why `count_field` is carried in the schema.
"""
from __future__ import annotations

from enum import IntEnum
from typing import Any

from . import bootstrap  # noqa: F401  -- must precede every `core` import

import core.IRS.constants as _constants
from core.IRS.bitfields import BitField
from core.IRS.core import ArrayField, Structure
from core.IRS.fields import BaseField, EnumField, Field

#: struct format char -> the name the IRS constants module gives it. Built from
#: `IRS.constants` rather than hardcoded, so a new type there shows up here for
#: free. `B` is both `UInt8` and `Byte`; first-wins keeps it at `UInt8`.
_DTYPE_BY_CHAR: dict[str, str] = {}
for _name, _fmt in vars(_constants).items():
    if not _name.startswith("_") and isinstance(_fmt, str) and len(_fmt) == 1:
        _DTYPE_BY_CHAR.setdefault(_fmt, _name)

_FLOAT_CHARS = frozenset("fd")
_BYTE_WIDTH = {"b": 1, "B": 1, "h": 2, "H": 2, "i": 4, "I": 4, "q": 8, "Q": 8, "f": 4, "d": 8}


def _format_char(packer) -> str:
    """The bare type char out of a `struct.Struct` format, minus byte-order."""
    return packer.format.lstrip("<>=!@")


#: `struct`'s byte-order prefixes. `=` and `@` mean native, which on every
#: platform this runs on is little-endian; an absent prefix means `@`.
_BIG_ENDIAN_PREFIXES = frozenset(">!")


def _byte_order(packer) -> str:
    """Which way round this field's bytes go, read off its own packer.

    Carried into the schema so the UI's byte view can render the wire order it
    will actually have instead of assuming one. Reading it here rather than
    hardcoding "little" costs a character comparison and means a layout that
    ever declares network order shows up correctly rather than reversed.
    """
    prefix = packer.format[:1]
    return "big" if prefix in _BIG_ENDIAN_PREFIXES else "little"


def _numeric_meta(char: str, endian: str = "little") -> dict[str, Any]:
    """dtype name, wire width, byte order, and the inclusive range -- so the UI
    can bound its inputs, reject a bad value before it ever reaches
    `struct.pack`, total up the message length it is about to send, and show
    the bytes that message will occupy."""
    width = _BYTE_WIDTH.get(char, 1)
    meta: dict[str, Any] = {
        "dtype": _DTYPE_BY_CHAR.get(char, char),
        "byte_size": width,
        "endian": endian,
    }
    if char in _FLOAT_CHARS:
        meta["numeric"] = "float"
        return meta
    bits = width * 8
    signed = char.islower()
    meta["numeric"] = "int"
    meta["min"] = -(1 << (bits - 1)) if signed else 0
    meta["max"] = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
    return meta


def _enum_options(enum_class: type[IntEnum]) -> list[dict[str, Any]]:
    return [{"name": member.name, "value": member.value} for member in enum_class]


def _bitfield_schema(name: str, bitfield: BitField) -> dict[str, Any]:
    """A BitField packs several small ints into one integer. `_fields_` is
    (name, shift, bits) and `__enum_map__` names the ones that are enums."""
    cls = type(bitfield)
    enum_map = getattr(cls, "__enum_map__", {})
    bits: list[dict[str, Any]] = []
    for bit_name, shift, width in cls._fields_:
        entry: dict[str, Any] = {
            "name": bit_name,
            "shift": shift,
            "bits": width,
            "min": 0,
            "max": (1 << width) - 1,
        }
        enum_class = enum_map.get(bit_name)
        if enum_class is not None:
            entry["kind"] = "enum"
            entry["enum"] = enum_class.__name__
            entry["options"] = _enum_options(enum_class)
        else:
            entry["kind"] = "scalar"
            entry["numeric"] = "int"
        bits.append(entry)
    # The whole bitfield occupies one packed integer, whose width comes from
    # its @baseType -- not from the sum of its bit widths, which may be less.
    packer = getattr(cls, "_packer_", None)
    return {
        "name": name,
        "kind": "bitfield",
        "struct": cls.__name__,
        "byte_size": packer.size if packer is not None else 1,
        "endian": _byte_order(packer) if packer is not None else "little",
        "bits": bits,
    }


def describe_field(field: BaseField, name: str | None = None) -> dict[str, Any]:
    """One `_fields_` entry -> one schema node. Recursive for struct/array."""
    field_name = name if name is not None else field._name

    # Order matters: BitField and Structure are both BaseField subclasses and
    # ArrayField is checked before them only because it is never either.
    if isinstance(field, ArrayField):
        item = describe_field(field.baseType, name="item")
        node: dict[str, Any] = {"name": field_name, "kind": "array", "item": item}
        if field.length is None:
            node["length"] = "dynamic"
        elif isinstance(field.length, str):
            node["length"] = "counted"
            node["count_field"] = field.length
        else:
            node["length"] = "fixed"
            node["size"] = int(field.length)
        # What the "Add <X>" button should say.
        node["item_label"] = item.get("struct") or item.get("dtype") or "item"
        return node

    if isinstance(field, BitField):
        return _bitfield_schema(field_name, field)

    if isinstance(field, Structure):
        return {
            "name": field_name,
            "kind": "struct",
            "struct": type(field).__name__,
            "fields": [describe_field(sub) for sub in type(field)._fields_],
        }

    if isinstance(field, EnumField):
        return {
            "name": field_name,
            "kind": "enum",
            "enum": field.enum_class.__name__,
            "options": _enum_options(field.enum_class),
            **_numeric_meta(_format_char(field.packer), _byte_order(field.packer)),
        }

    if isinstance(field, Field):
        return {
            "name": field_name,
            "kind": "scalar",
            **_numeric_meta(_format_char(field.packer), _byte_order(field.packer)),
        }

    # An IRS field kind this module has not been taught yet. Surface it as
    # read-only rather than guessing at a widget for it.
    return {"name": field_name, "kind": "unsupported", "python_type": type(field).__name__}


def describe_message(message_class: type, unit_code: int, op_code: int) -> dict[str, Any]:
    """Full form schema for one registered (unitCode, opCode) message."""
    return {
        "unit_code": unit_code,
        "op_code": op_code,
        "name": message_class.__name__,
        "doc": (message_class.__doc__ or "").strip() or None,
        "fields": [describe_field(field) for field in message_class._fields_],
    }


#: Field kinds a filter RULE can compare against. A rule needs a single value to
#: put on one side of an operator, which a struct, a bitfield-as-a-whole, or an
#: array does not have.
_COMPARABLE = frozenset({"scalar", "enum"})


def filter_targets(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Every field of one message a received-filter may address, flattened.

    Two different questions, answered per node, because the two halves of a
    filter can address different things:

    `rule_ok`   -- may a keep/drop rule compare this? Only a single decoded
                   value: a scalar, an enum, or one bit of a bitfield.
    `change_ok` -- may the change trigger watch this? Anything reachable,
                   including a whole struct, a whole bitfield, or an array --
                   the trigger compares by value, and `==` on a list or a dict
                   is exactly the right question there.

    **The walk never descends into an array's item.** A dotted path cannot name
    one of 35 elements, and silently addressing the first would be a lie the
    user could not see -- so an array is offered as a change subject (compare
    the whole list) and nothing inside it is offered at all. This is the one
    place that limitation is decided; `filters.py` only ever sees paths.

    Paths use the same dotted grammar as the byte view's `leafOffsets`
    (`Position.Latitude`), so a field is spelt one way across the whole UI.
    """
    targets: list[dict[str, Any]] = []

    def emit(path: list[str], node: dict[str, Any], rule_ok: bool) -> None:
        target: dict[str, Any] = {
            "path": ".".join(path),
            "name": path[-1],
            "kind": node.get("kind", "scalar"),
            "rule_ok": rule_ok,
        }
        for key in ("dtype", "enum", "options", "min", "max", "numeric", "struct",
                    "item_label", "length"):
            if key in node:
                target[key] = node[key]
        targets.append(target)

    def walk(node: dict[str, Any], path: list[str]) -> None:
        kind = node.get("kind")
        if kind == "struct":
            emit(path, node, rule_ok=False)
            for child in node.get("fields", []):
                walk(child, [*path, child["name"]])
            return
        if kind == "bitfield":
            emit(path, node, rule_ok=False)
            for bit in node.get("bits", []):
                # A bit IS comparable -- `BitField.to_dict` emits one key per
                # bit, with enum bits as member names, so `Area.fr` resolves.
                emit([*path, bit["name"]], bit, rule_ok=True)
            return
        if kind == "array":
            emit(path, node, rule_ok=False)     # watchable, never rule-addressable
            return
        emit(path, node, rule_ok=kind in _COMPARABLE)

    for field_node in schema.get("fields", []):
        walk(field_node, [field_node["name"]])
    return targets
