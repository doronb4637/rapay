"""
Normalise a form payload into something `core` can encode without surprises.

Two concrete failure modes in `core`/`IRS` make this mandatory rather than
cosmetic, both verified against the real encoder:

1. **A missing field is not a clean error.** `Structure.from_dict` only assigns
   keys that are present (`if field.name in data`), leaving the slot unset;
   `to_bytes` then does `getattr(target, field.name)` and blows up with a bare
   `AttributeError: 'SetGeneralFlag' object has no attribute 'value'` from deep
   inside IRS. `BitField.from_dict` is stricter still -- `data[name]` raises
   `KeyError`. Zero-filling every absent field here turns both into a
   well-formed message, which is what the "empty fields default to 0" rule in
   the GSim spec is really for.

2. **A counted array's length field is not maintained by IRS.**
   `ArrayField.to_bytes` iterates the list and never writes the count;
   `from_bytes` reads exactly `getattr(instance, count_field)` items. If the two
   disagree nothing raises -- the receiver simply mis-parses. So GSim owns
   keeping them in sync, on every send.

Both run against the schema from `schema.py`, so they cover arbitrary nesting.
"""
from __future__ import annotations

from typing import Any


def _default_for(node: dict[str, Any]) -> Any:
    """The zero value for one schema node."""
    kind = node["kind"]
    if kind == "scalar":
        return 0.0 if node.get("numeric") == "float" else 0
    if kind == "enum":
        options = node.get("options") or []
        # Prefer a member whose value is literally 0 (the conventional
        # "unset"/"unknown" member); otherwise the first declared member, since
        # EnumField.from_bytes rejects any value with no member at all.
        for option in options:
            if option["value"] == 0:
                return option["value"]
        return options[0]["value"] if options else 0
    if kind == "struct":
        return {sub["name"]: _default_for(sub) for sub in node["fields"]}
    if kind == "bitfield":
        return {bit["name"]: _default_for(bit) for bit in node["bits"]}
    if kind == "array":
        if node.get("length") == "fixed":
            return [_default_for(node["item"]) for _ in range(node.get("size", 0))]
        return []
    return 0


def normalise(fields: list[dict[str, Any]], payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of `payload` with every field in `fields` present and
    every counted array's length field agreeing with its list.

    Never mutates the caller's dict; unknown keys are dropped, because anything
    IRS does not declare cannot be encoded anyway.
    """
    payload = payload or {}
    result: dict[str, Any] = {}

    for node in fields:
        name = node["name"]
        value = payload.get(name)
        kind = node["kind"]

        if value is None or value == "":
            result[name] = _default_for(node)
            continue

        if kind == "struct":
            result[name] = normalise(node["fields"], value if isinstance(value, dict) else {})
        elif kind == "bitfield":
            result[name] = normalise(node["bits"], value if isinstance(value, dict) else {})
        elif kind == "array":
            items = value if isinstance(value, list) else []
            item_node = node["item"]
            if node.get("length") == "fixed":
                size = node.get("size", 0)
                items = (items + [None] * size)[:size]
            result[name] = [_normalise_item(item_node, item) for item in items]
        else:
            result[name] = value

    # Second pass: a counted array's length lives in a SIBLING field, so it can
    # only be reconciled once every sibling has been materialised above.
    for node in fields:
        if node["kind"] == "array" and node.get("length") == "counted":
            count_field = node.get("count_field")
            if count_field in result:
                result[count_field] = len(result[node["name"]])

    return result


def _normalise_item(item_node: dict[str, Any], value: Any) -> Any:
    """One array element, which may itself be a struct/bitfield/array."""
    kind = item_node["kind"]
    if kind == "struct":
        return normalise(item_node["fields"], value if isinstance(value, dict) else {})
    if kind == "bitfield":
        return normalise(item_node["bits"], value if isinstance(value, dict) else {})
    if kind == "array":
        items = value if isinstance(value, list) else []
        return [_normalise_item(item_node["item"], item) for item in items]
    if value is None or value == "":
        return _default_for(item_node)
    return value


def build_payload(schema: dict[str, Any], raw: dict[str, Any] | None) -> dict[str, Any]:
    """Public entry point: schema (from `registry.message_schema`) + whatever
    the form submitted -> a dict safe to hand to `Connection.send_message`.

    No conversion to an IRS object is needed here: `irs_to_bytes` accepts a
    plain dict and calls `message_class.from_dict()` itself, and
    `Connection._encode` passes any non-`bytes` straight through to it.
    """
    return normalise(schema["fields"], raw)
