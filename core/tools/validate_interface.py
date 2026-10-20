"""
Offline validator for IRS structures files and DDS Interfaces.

The library trusts these files as written and checks none of their content at
load, send or receive (root `CLAUDE.md`, "Structures Files Are Correct By
Definition"). This script is where that checking lives instead. Run it after
changing the code that generates the files, or after editing one by hand;
nothing in `core` or `gsim` imports it.

Usage, from the repo root -- each argument is a `.py` path or a dotted module
name, exactly as a connection config's `Structures` / `dds_interface` names it:

    .venv/Scripts/python.exe -m core.tools.validate_interface path/to/structures.py
    .venv/Scripts/python.exe -m core.tools.validate_interface core.IRS.Structures.Test.test_messages
    .venv/Scripts/python.exe -m core.tools.validate_interface path/to/dds_interface.py other.py

A file defining any `DdsUnit` is checked as a DDS Interface, anything else as an
IRS structures file. Every problem found is printed; the exit status is 1 if
any file has one, else 0.

IRS structures file checks:
  * it imports at all (a field annotated with no Type/Size fails here; the
    traceback points at the class);
  * `ENDIAN`, if declared, is `bigEndian` or `littleEndian`;
  * it defines at least one `@Definition` message (none usually means it reached
    a second IRS copy, see `core/IRS/_alias.py`);
  * every unit code fits the UInt8 header field and every opcode the UInt16 one;
  * no two messages share one (unitCode, opCode) route;
  * every field of a message/structure is a real wire field;
  * a counted array's length names an earlier field of the same class;
  * every `BitField` has an `@baseType`, and its bits fit inside it;
  * `PAIRS` is a dict of int -> int, a pair code defines no messages of its
    own, and the unit it speaks as sends at least one message in this file.

DDS Interface checks (needs `rti.connextdds`, as loading the Interface does):
  * it imports at all;
  * every unit has its own unit code;
  * no two topic classes share one wire type name;
  * a topic a unit subscribes while two or more peers publish it, or while
    that unit publishes it too, has a type carrying `A_sourceID.A_systemId`.
"""
from __future__ import annotations

import sys
import traceback
from types import ModuleType

from core.IRS import BitField, Message, Structure, is_definition
from core.IRS.constants import ENDIAN_ATTRIBUTE, big_endian, little_endian
from core.IRS.core import ArrayField
from core.IRS.fields import BaseField
from core.IRS.irs_parser import PAIRS_ATTRIBUTE
from core.tools.general import import_modules


def validate(spec: str) -> list[str]:
    """Load the structures file or DDS Interface `spec` and return every problem in it.

    Args:
        spec: a `.py` path or a dotted module name, as a connection config gives it.

    Returns:
        One human-readable line per problem; an empty list means the file is valid.
    """
    try:
        module = import_modules(spec)[0]
    except Exception:  # noqa: BLE001 - a file that cannot import is the first problem to report
        return [f"failed to import:\n{traceback.format_exc()}"]
    if any(type(value).__name__ == "DdsUnit" for value in vars(module).values()):
        return _dds_problems(module)
    return _irs_problems(module)


def _defined_here(module: ModuleType, base: type) -> list[type]:
    """Subclasses of `base` that `module` itself defines, not ones it imports."""
    return [value for value in vars(module).values()
            if isinstance(value, type) and issubclass(value, base) and value.__module__ == module.__name__]


def _irs_problems(module: ModuleType) -> list[str]:
    """Every problem in an IRS structures module."""
    problems = []
    endian = getattr(module, ENDIAN_ATTRIBUTE, little_endian)
    if endian not in (little_endian, big_endian):
        problems.append(f"{ENDIAN_ATTRIBUTE} must be bigEndian ('>') or littleEndian ('<'), got {endian!r}")

    messages = [value for name, value in vars(module).items() if not name.startswith("_") and is_definition(value)]
    if not messages:
        problems.append("defines no @Definition message (or its classes come from a second IRS copy, "
                        "see core/IRS/_alias.py)")
    routes: dict[tuple[int, int], type[Message]] = {}
    for message in messages:
        unit, op = message._unitCode, message._opCode
        if not 0 <= unit <= 0xFF:
            problems.append(f"{message.__name__}: unitCode {unit!r} does not fit the UInt8 header field")
        if not 0 <= op <= 0xFFFF:
            problems.append(f"{message.__name__}: opCode {op!r} does not fit the UInt16 header field")
        other = routes.setdefault((unit, op), message)
        if other is not message:
            problems.append(f"(unitCode={unit}, opCode={op}) is both {other.__name__} and {message.__name__}")

    for structure in _defined_here(module, Structure):
        names = [field._name for field in structure._fields_]
        for index, field in enumerate(structure._fields_):
            if not isinstance(field, BaseField):
                problems.append(f"{structure.__name__}.{names[index]}: annotated with no Type/Size")
            elif isinstance(field, ArrayField) and isinstance(field.length, str) and field.length not in names[:index]:
                problems.append(f"{structure.__name__}.{field._name}: length {field.length!r} "
                                f"is not an earlier field of {structure.__name__}")

    for bitfield in _defined_here(module, BitField):
        if bitfield._packer_ is None:
            problems.append(f"BitField {bitfield.__name__} has no @baseType")
            continue
        bits = sum(width for _, _, width in bitfield._fields_)
        allocated = bitfield._packer_.size * 8
        if bits > allocated:
            problems.append(f"BitField {bitfield.__name__} defines {bits} bits, "
                            f"but its @baseType only allocates {allocated}")

    pairs = getattr(module, PAIRS_ATTRIBUTE, {})
    # `type(...) is int` because True is an int but never a unit code.
    if not isinstance(pairs, dict) or not all(type(code) is int and type(speaks) is int for code, speaks in pairs.items()):
        problems.append(f"{PAIRS_ATTRIBUTE} must be a dict of {{pair unit code: unit code it speaks as}}, got {pairs!r}")
        return problems
    for pair_code, speaks_code in pairs.items():
        if any(unit == pair_code for unit, _op in routes):
            problems.append(f"unit {pair_code} is paired to {speaks_code} but also defines messages of its own")
        if not any(unit == speaks_code for unit, _op in routes):
            problems.append(f"unit {pair_code} is paired to {speaks_code}, which sends no message in this file")
    return problems


def _carries_source(sample_type: type) -> bool:
    """Whether a default `sample_type` sample has `A_sourceID.A_systemId`."""
    # Imported here: it pulls in rti, which only a DDS Interface needs.
    from core.connections.dds import SOURCE_ID_FIELD, SYSTEM_ID_FIELD
    source_id = getattr(sample_type(), SOURCE_ID_FIELD, None)
    return source_id is not None and hasattr(source_id, SYSTEM_ID_FIELD)


def _dds_problems(module: ModuleType) -> list[str]:
    """Every problem in a DDS Interface module."""
    problems = []
    units = {name: value for name, value in vars(module).items() if type(value).__name__ == "DdsUnit"}

    code_owner: dict[int, str] = {}
    for name, unit in units.items():
        owner = code_owner.setdefault(unit.unitCode, name)
        if owner != name:
            problems.append(f"units {owner} and {name} both use unitCode {unit.unitCode:#04x}")

    type_owner: dict[str, type] = {}
    for cls in dict.fromkeys(cls for unit in units.values() for cls in (*unit.publish, *unit.subscribe)):
        type_name = cls.type_support.type_name
        owner = type_owner.setdefault(type_name, cls)
        if owner is not cls:
            problems.append(f"{owner.__qualname__} and {cls.__qualname__} share the wire type name {type_name!r}")

    for name, unit in units.items():
        for cls in unit.subscribe:
            publishers = [other for other, peer in units.items() if other != name and cls in peer.publish]
            hears_itself = cls in unit.publish
            if (len(publishers) < 2 and not hears_itself) or _carries_source(cls):
                continue
            why = ("also publishes it, and a participant hears its own writes" if hears_itself
                   else f"it has {len(publishers)} publishers {publishers}")
            problems.append(f"unit {name} subscribes {cls.__qualname__} and {why}, but the type has no "
                            f"A_sourceID.A_systemId to tell the senders apart")
    return problems


def main(argv: list[str]) -> int:
    """Validate every file named in `argv`, print the result, and return the exit status."""
    if not argv:
        print(__doc__)
        return 2
    failed = False
    for spec in argv:
        problems = validate(spec)
        print(f"{'FAIL' if problems else 'OK  '} {spec}")
        for problem in problems:
            print(f"  - {problem}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
