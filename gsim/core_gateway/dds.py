"""
DDS topics as GSim messages: what the Messages panel lists, what the Inspector
renders a form from, and how a form payload becomes a sample.

A DDS route key is the topic NAME (a `str`), where IRS uses an `int` opcode --
which is exactly how core's own `send_message` / `handle_on_receive` take it
for a DDS connection, so nothing here translates between the two.

The only GSim module that touches `rti`, and only lazily: GSim must still start
on a machine without Connext, where core already disables DDS itself.

Introspection reads `type_support.dynamic_type`, the type Connext itself
serialises from, rather than the dataclass annotations -- bounds and array
dimensions live only there. Sample <-> dict goes through Connext's own JSON
codec, so the dataclass defaults own every field the form left out, the way
IRS's `fill()` does for a framed message.
"""
from __future__ import annotations

import json
from typing import Any

from . import bootstrap  # noqa: F401  -- must precede every `core` import

from core.connections.dds_config import (
    DEFAULT_DOMAIN_ID,
    DEFAULT_QOS_FILE,
    DdsUnitConfig,
    TopicSpec,
    load_dds_interface,
)

from .payloads import Prepared


def dds_defaults() -> dict[str, Any]:
    """What core uses when a DDS config leaves these out."""
    return {"domain_id": DEFAULT_DOMAIN_ID, "qos_file": str(DEFAULT_QOS_FILE)}


def _interface_units(interface: str) -> dict[str, Any]:
    """{unit name: DdsUnit}, found the way core's loader finds them."""
    module = load_dds_interface(interface)
    return {name: value for name, value in vars(module).items()
            if type(value).__name__ == "DdsUnit"}


def dds_units(interface: str) -> dict[str, Any]:
    """Every unit an Interface defines, so the connection modal can offer them
    as choices instead of asking for a name to be typed."""
    module = load_dds_interface(interface)
    units = _interface_units(interface)
    return {
        "system": str(getattr(module, "SYSTEM", None) or module.__name__),
        "units": [
            {
                "name": name,
                "unit_code": unit.unitCode,
                "publish": [cls.type_support.type_name for cls in unit.publish],
                "subscribe": [cls.type_support.type_name for cls in unit.subscribe],
            }
            for name, unit in units.items()
        ],
    }


def topic_class(interface: str, topic: str) -> type:
    """The class carrying `topic` in an Interface -- the lookup a log entry's
    schema needs, since it carries only the interface and the topic name."""
    for unit in _interface_units(interface).values():
        for cls in (*unit.publish, *unit.subscribe):
            if cls.type_support.type_name == topic:
                return cls
    raise KeyError(f"DDS Interface {interface!r} has no topic {topic!r}")


# -- listing -------------------------------------------------------------------
def _summary(spec: TopicSpec, unit_code: int, interface: str) -> dict[str, Any]:
    return {
        "unit_code": unit_code,
        "op_code": spec.name,
        "op_code_hex": spec.name,
        "name": spec.name,
        "field_count": len(spec.sample_type.type_support.dynamic_type.members()),
        "namespace": interface,
    }


def outgoing(dds: DdsUnitConfig, unit_name: str | None) -> list[dict[str, Any]]:
    """Topics this unit publishes that `unit_name` subscribes (all published
    topics when `unit_name` is None)."""
    return [
        _summary(spec, dds.unit_code, dds.interface)
        for spec in dds.topics
        if spec.publishes and (unit_name is None or unit_name in spec.subscribers)
    ]


def incoming(dds: DdsUnitConfig, unit_name: str, unit_code: int) -> list[dict[str, Any]]:
    """Topics this unit subscribes that `unit_name` publishes."""
    return [
        _summary(spec, unit_code, dds.interface)
        for spec in dds.topics
        if spec.subscribes and unit_name in spec.publishers
    ]


# -- schema --------------------------------------------------------------------
def _int_range(bits: int, signed: bool) -> dict[str, Any]:
    if signed:
        return {"numeric": "int", "min": -(1 << (bits - 1)), "max": (1 << (bits - 1)) - 1}
    return {"numeric": "int", "min": 0, "max": (1 << bits) - 1}


def _primitive_meta(kind: Any) -> dict[str, Any] | None:
    import rti.connextdds as rti_dds

    tk = rti_dds.TypeKind
    # A list, not a dict: `TypeKind` is unhashable.
    table = (
        (tk.INT_8_TYPE, "Int8", 1, _int_range(8, True)),
        (tk.UINT_8_TYPE, "UInt8", 1, _int_range(8, False)),
        (tk.OCTET_TYPE, "UInt8", 1, _int_range(8, False)),
        (tk.CHAR_8_TYPE, "Char8", 1, _int_range(8, False)),
        (tk.INT_16_TYPE, "Int16", 2, _int_range(16, True)),
        (tk.UINT_16_TYPE, "UInt16", 2, _int_range(16, False)),
        (tk.INT_32_TYPE, "Int32", 4, _int_range(32, True)),
        (tk.UINT_32_TYPE, "UInt32", 4, _int_range(32, False)),
        (tk.INT_64_TYPE, "Int64", 8, _int_range(64, True)),
        (tk.UINT_64_TYPE, "UInt64", 8, _int_range(64, False)),
        (tk.FLOAT_32_TYPE, "Float32", 4, {"numeric": "float"}),
        (tk.FLOAT_64_TYPE, "Float64", 8, {"numeric": "float"}),
    )
    for candidate, dtype, size, meta in table:
        if kind == candidate:
            # CDR is little-endian on every platform GSim runs on; the byte
            # view is off for DDS anyway (see `describe_topic`).
            return {"dtype": dtype, "byte_size": size, "endian": "little", **meta}
    return None


def _describe_type(name: str, dyn: Any) -> dict[str, Any]:
    import rti.connextdds as rti_dds

    tk = rti_dds.TypeKind
    kind = dyn.kind
    if kind == tk.ALIAS_TYPE:
        return _describe_type(name, dyn.related_type)
    if kind == tk.STRUCTURE_TYPE:
        return {"name": name, "kind": "struct", "struct": dyn.name,
                "fields": [_describe_type(m.name, m.type) for m in dyn.members()]}
    if kind == tk.ENUMERATION_TYPE:
        return {"name": name, "kind": "enum", "enum": dyn.name,
                "options": [{"name": m.name, "value": m.ordinal} for m in dyn.members()],
                **_int_range(32, True), "dtype": "Enum", "byte_size": 4, "endian": "little"}
    if kind == tk.BOOLEAN_TYPE:
        return {"name": name, "kind": "bool", "dtype": "Bool", "byte_size": 1}
    if kind == tk.STRING_TYPE:
        node: dict[str, Any] = {"name": name, "kind": "string", "dtype": "String"}
        if dyn.bounds != dyn.UNBOUNDED:
            node["max_length"] = dyn.bounds
        return node
    # `==`, never `in`: `TypeKind` membership tests come back False.
    if kind == tk.ARRAY_TYPE or kind == tk.SEQUENCE_TYPE:
        item = _describe_type("item", dyn.content_type)
        node = {"name": name, "kind": "array", "item": item,
                "item_label": item.get("struct") or item.get("dtype") or "item"}
        if kind == tk.ARRAY_TYPE:
            # Multi-dimensional arrays travel flattened, row-major, which is
            # also how Connext's JSON spells them.
            node.update(length="fixed", size=dyn.total_element_count)
        else:
            # `sequence` tells the UI this is a DDS sequence, not an IRS array
            # that fills the rest of a frame -- both are growable lists.
            node.update(length="dynamic", sequence=True)
            if dyn.bounds != dyn.UNBOUNDED:
                node["max_items"] = dyn.bounds
        return node
    meta = _primitive_meta(kind)
    if meta is not None:
        return {"name": name, "kind": "scalar", **meta}
    return {"name": name, "kind": "unsupported", "python_type": str(kind)}


def describe_topic(cls: type, unit_code: int, interface: str) -> dict[str, Any]:
    """Form schema for one topic, in the node kinds `schema.py` emits for IRS
    plus `string` and `bool`, which IRS has no equivalent of."""
    dyn = cls.type_support.dynamic_type
    doc = (cls.__doc__ or "").strip()
    # `@idl.struct` is a dataclass, which invents a signature-shaped docstring
    # for any class that has none of its own.
    if doc.startswith(f"{cls.__name__}("):
        doc = ""
    return {
        "unit_code": unit_code,
        "op_code": cls.type_support.type_name,
        "name": cls.type_support.type_name,
        "doc": doc or None,
        "namespace": interface,
        # The UI's byte view mirrors the IRS layout; a CDR layout (alignment,
        # encapsulation header, length-prefixed strings) is a different thing.
        "byte_view": False,
        "fields": [_describe_type(m.name, m.type) for m in dyn.members()],
    }


# -- samples -------------------------------------------------------------------
def _blank(value: Any) -> bool:
    return value is None or value == ""


def _prepare(node: dict[str, Any], value: Any, path: str) -> Any:
    """One form value, cleaned for Connext's JSON parser. A struct member the
    form left blank is omitted, so the type's own default fills it."""
    kind = node["kind"]
    if kind == "struct":
        source = value if isinstance(value, dict) else {}
        out = {}
        for child in node["fields"]:
            item = source.get(child["name"])
            if _blank(item):
                continue
            out[child["name"]] = _prepare(child, item, f"{path}.{child['name']}")
        return out
    if kind == "array":
        items = list(value) if isinstance(value, list) else []
        item_node = node["item"]
        if node["length"] == "fixed":
            # Forgiving about how many rows a form filled in, as for IRS. A
            # short JSON array would leave the tail at its default anyway, but
            # an over-long one is a parse error with no field named.
            items = items[:node["size"]]
            items += [None] * (node["size"] - len(items))
        elif "max_items" in node and len(items) > node["max_items"]:
            raise ValueError(
                f"{path}: {len(items)} items exceed the sequence bound {node['max_items']}")
        return [_prepare(item_node, item, f"{path}[{index}]") if not _blank(item)
                else _filler(item_node)
                for index, item in enumerate(items)]
    if kind == "string":
        text = str(value)
        if "max_length" in node and len(text) > node["max_length"]:
            raise ValueError(f"{path}: {len(text)} characters exceed the bound {node['max_length']}")
        return text
    return value


def _filler(node: dict[str, Any]) -> Any:
    """One element for an array slot the form left blank. An element is a
    value in a list, so there is no absent slot for the type's default to fill."""
    kind = node["kind"]
    if kind == "struct":
        return {}
    if kind == "array":
        return [_filler(node["item"])] * node["size"] if node["length"] == "fixed" else []
    if kind == "string":
        return ""
    if kind == "bool":
        return False
    if kind == "enum":
        return node["options"][0]["name"] if node["options"] else 0
    return 0


def sample_to_dict(sample: Any) -> dict[str, Any]:
    """A sample in `to_dict()` shape: nested dicts, enums as member names."""
    return json.loads(type(sample).type_support.to_json(sample))


def prepare_sample(cls: type, interface: str, raw: dict[str, Any] | None) -> Prepared:
    """A form payload -> a sample of `cls`, plus its canonical dict for the log.

    Raises `ValueError` for anything Connext refuses, with the reason it gives.
    """
    schema = describe_topic(cls, 0, interface)
    cleaned = _prepare({"kind": "struct", "fields": schema["fields"]}, raw or {}, schema["name"])
    try:
        sample = cls.type_support.from_json(json.dumps(cleaned))
    except Exception as exc:  # noqa: BLE001 - Connext raises its own hierarchy
        raise ValueError(f"{schema['name']}: Connext refused the payload: {exc}") from exc
    return Prepared(sample, schema["name"], interface, sample_to_dict(sample))
