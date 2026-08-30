"""The compiled parser must be indistinguishable from the interpreted one.

`core/IRS/_compiler.py` generates a function per message class and installs it
over `Structure.from_bytes` / `to_bytes`. The `_fields_` loops it replaces are
still there and are still the definition of correct -- so the way to test the
generator is not to re-derive what it should emit, but to run BOTH and demand
the same answer.

That comparison has to happen in two PROCESSES, not two calls. Compilation is
per class and sticky, and the interpreted path reaches nested structures through
`self.baseType.from_bytes` -- which would find the compiled classmethod and quietly
make the "interpreted" run half-compiled. `IRS_COMPILE=0` disables the generator
process-wide, so a subprocess per mode is the only honest oracle.

The probe below runs identically under both, over every layout the repo defines
and a fixed set of adversarial buffers (empty, truncated mid-field, exact fit,
overlong, all-zero, all-0xFF, seeded random).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Buffer lengths worth trying against every layout: 0 and 1 catch the empty and
#: one-byte cases, the rest straddle typical field and message boundaries so that
#: some runs truncate mid-field, some fit exactly, and some leave a tail over.
_LENGTHS = (0, 1, 2, 3, 4, 5, 7, 8, 11, 16, 21, 32, 44, 45, 55, 64, 92, 99, 128)


def _payloads(length: int) -> list[bytes]:
    """Deterministic buffers of `length` bytes -- same in both processes."""
    import random
    generator = random.Random(0xC0FFEE + length)
    return [
        bytes(length),
        b"\xff" * length,
        bytes(generator.randrange(256) for _ in range(length)),
        bytes(range(length % 256)) if length <= 256 else bytes(length),
    ]


def _layouts() -> list[tuple[str, type]]:
    """Every message class this repo defines, by a stable name."""
    from core.IRS.REGISTRY import STRUCTURE_REGISTRY

    import core.IRS.Structures.Test.test_messages  # noqa: F401  -- registers
    import core.IRS.Structures.Tiful.tiful_to_dtu  # noqa: F401  -- registers
    import core.tests._messages  # noqa: F401      -- registers
    import core.tests._messages_big_endian as big_endian
    from core.IRS.benchmark.bench_message_parsing import BenchMessage, NestedMessage
    from core.IRS.core import Message, Structure

    # The benchmark's two layouts register no route, but between them they are
    # the only place every field kind meets: BenchMessage covers the scalars,
    # NestedMessage covers arrays OF STRUCTURES -- the most involved thing the
    # generator emits, and the one with no example among the real specs.
    found: dict[str, type] = {"BenchMessage": BenchMessage, "NestedMessage": NestedMessage}
    for units in STRUCTURE_REGISTRY.values():
        for opcodes in units.values():
            for message_class in opcodes.values():
                found[f"{message_class.__module__}.{message_class.__qualname__}"] = message_class
    # The big-endian twins register nothing -- they exist to be embedded -- so
    # they are picked up off the module rather than out of the registry. Only
    # Structure subclasses: a BitField also has a `_fields_`, but its entries are
    # (name, shift, bits) tuples rather than field objects.
    for name in dir(big_endian):
        attribute = getattr(big_endian, name)
        if (isinstance(attribute, type) and issubclass(attribute, Structure)
                and attribute not in (Structure, Message) and attribute._fields_):
            found[f"big_endian.{name}"] = attribute
    return sorted(found.items())


def _canonical(value):
    """`to_dict` output plus the container types `to_dict` erases."""
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [type(value).__name__, [_canonical(item) for item in value]]
    return value


def probe() -> None:
    """Print one JSON record per (layout, buffer). Runs under both modes."""
    from core.IRS.buffers import BinaryReader

    results = {}
    for label, message_class in _layouts():
        for length in _LENGTHS:
            for index, payload in enumerate(_payloads(length)):
                key = f"{label}|{length}|{index}"
                try:
                    message = message_class.from_bytes(BinaryReader(payload))
                except Exception as error:  # noqa: BLE001 -- the outcome IS the datum
                    results[key] = {"raised": type(error).__name__}
                    continue
                record = {"dict": _canonical(message.to_dict())}
                # `to_dict` renders arrays as plain lists, so container identity
                # is captured separately -- that is where FixedList shows up.
                record["containers"] = {
                    field._name: type(getattr(message, field._name)).__name__
                    for field in message_class._fields_
                    if isinstance(getattr(message, field._name, None), list)
                }
                try:
                    record["bytes"] = message.to_bytes().hex()
                except Exception as error:  # noqa: BLE001
                    record["bytes"] = f"raised:{type(error).__name__}"
                results[key] = record
    json.dump(results, sys.stdout, sort_keys=True)


def _run(compile_enabled: bool) -> dict:
    environment = dict(os.environ, IRS_COMPILE="1" if compile_enabled else "0",
                       PYTHONPATH=str(REPO_ROOT))
    completed = subprocess.run(
        [sys.executable, "-c",
         "from core.tests.test_irs_compiler import probe; probe()"],
        cwd=REPO_ROOT, env=environment, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def both_modes() -> tuple[dict, dict]:
    return _run(compile_enabled=False), _run(compile_enabled=True)


def test_probe_covers_every_layout(both_modes):
    """Guards the test itself: an empty probe would pass every assertion below."""
    interpreted, _ = both_modes
    labels = {key.split("|")[0] for key in interpreted}
    assert len(labels) >= 10, sorted(labels)
    assert len(interpreted) >= 500, len(interpreted)
    for expected in ("BenchMessage", "NestedMessage", "big_endian.Sample",
                     "tiful_to_dtu.StructMessage", "tiful_to_dtu.ArrayOfAreas"):
        assert any(label.endswith(expected) for label in labels), sorted(labels)


def test_same_keys(both_modes):
    interpreted, compiled = both_modes
    assert interpreted.keys() == compiled.keys()


def test_parsed_values_are_identical(both_modes):
    """Every buffer that parses must parse to the same message, both ways."""
    interpreted, compiled = both_modes
    parsed = 0
    for key, expected in interpreted.items():
        if "raised" in expected:
            continue
        actual = compiled[key]
        assert "raised" not in actual, f"{key}: compiled rejected what interpreted accepted: {actual}"
        assert actual["dict"] == expected["dict"], key
        parsed += 1
    assert parsed >= 200, parsed


def test_serialization_is_byte_identical(both_modes):
    interpreted, compiled = both_modes
    for key, expected in interpreted.items():
        if "raised" in expected:
            continue
        assert compiled[key]["bytes"] == expected["bytes"], key


def test_rejections_agree(both_modes):
    """A buffer rejected one way is rejected the other, for a comparable reason.

    The exception TYPE may legitimately differ in one direction: the compiled
    parser unpacks a whole fixed block up front, so a buffer that is long enough
    to reach an undefined enum value but too short to finish the block reports
    the truncation (`struct.error`) where the interpreted parser reported the
    enum (`ValueError`). Both refuse the buffer, which is the contract.
    """
    interpreted, compiled = both_modes
    comparable = {"error", "ValueError"}
    for key, expected in interpreted.items():
        if "raised" not in expected:
            continue
        actual = compiled[key]
        assert "raised" in actual, f"{key}: compiled accepted what interpreted rejected"
        if actual["raised"] != expected["raised"]:
            assert {actual["raised"], expected["raised"]} <= comparable, (
                key, expected["raised"], actual["raised"])


def test_fixed_arrays_are_length_locked(both_modes):
    """A `[X, N]` field is a FixedList; counted and greedy fields are not."""
    _, compiled = both_modes
    seen_fixed = seen_free = 0
    for record in compiled.values():
        for container in record.get("containers", {}).values():
            if container == "FixedList":
                seen_fixed += 1
            elif container == "list":
                seen_free += 1
    assert seen_fixed, "no fixed-length array was exercised"
    assert seen_free, "no counted/greedy array was exercised"


""" In-process checks that do not need the two-mode comparison """
def test_fixedlist_allows_edits_and_refuses_resizing():
    from beartype.door import is_bearable

    from core.IRS.containers import FixedList

    array = FixedList([1, 2, 3])
    assert isinstance(array, list) and is_bearable(array, list[int])
    array[0] = 9
    assert array == [9, 2, 3]
    array[0:2] = [7, 8]                       # same length -- fine
    assert array == [7, 8, 3]
    for operation in (lambda: array.append(4), lambda: array.extend([4]),
                      lambda: array.insert(0, 4), lambda: array.pop(),
                      lambda: array.remove(7), lambda: array.clear(),
                      lambda: array.__delitem__(0),
                      lambda: array.__setitem__(slice(0, 1), [1, 2])):
        with pytest.raises(TypeError, match="length cannot change"):
            operation()
    assert array == [7, 8, 3]


def test_fixedlist_survives_copy_and_pickle():
    """`list` copies by re-appending, which FixedList refuses -- so it overrides."""
    import copy
    import pickle

    from core.IRS.containers import FixedList

    array = FixedList([1, 2, 3])
    for clone in (copy.copy(array), copy.deepcopy(array), pickle.loads(pickle.dumps(array))):
        assert isinstance(clone, FixedList) and clone == [1, 2, 3]


def test_parsed_fixed_array_is_locked_but_counted_one_is_not():
    from core.IRS.buffers import BinaryReader
    from core.IRS.Structures.Tiful.tiful_to_dtu import ArrayOfAreas, StructMessage

    message = StructMessage.from_bytes(BinaryReader(bytes(99)))
    assert isinstance(message.datas, list)
    with pytest.raises(TypeError):
        message.datas.append(message.datas[0])
    message.datas[0].array[0] = 7             # editing a value stays legal
    assert message.datas[0].array[0] == 7

    counted = ArrayOfAreas.from_bytes(BinaryReader(bytes([2, 0, 0])))
    counted.Areas.append(counted.Areas[0])    # counted arrays are meant to grow
    assert len(counted.Areas) == 3


def test_fill_produces_a_locked_fixed_array():
    from core.IRS.Structures.Tiful.tiful_to_dtu import StructMessage

    filled = StructMessage().fill()
    assert len(filled.datas) == 9
    with pytest.raises(TypeError):
        filled.datas.append(filled.datas[0])
    assert len(filled.to_bytes()) == 99


def test_compile_all_compiles_every_layout():
    """The startup warm-up must not leave anything on the slow path."""
    from core.IRS import compile_all

    assert compile_all() > 0
    assert compile_all() > 0                  # idempotent


""" Structural shapes the repo's own layouts happen not to contain """
def _shapes():
    """Built lazily: defining a Message registers nothing, but it does compile."""
    from enum import IntEnum

    from core.IRS import Byte, Message, Structure, UInt16

    class Empty(Message):
        pass

    class OneField(Message):
        a: int = Byte

    class OnlyGreedy(Message):
        data: list[int] = [UInt16, None]      # greedy with a MULTI-BYTE element

    class ZeroLength(Message):
        a: int = Byte
        z: list[int] = [Byte, 0]              # a fixed array of nothing

    class Inner(Structure):
        tail: list[int] = [Byte, None]

    class VariableNested(Message):
        head: int = Byte
        body: Inner                           # a nested struct that is NOT fixed-size

    return [(Empty, b""), (OneField, b"\x07"), (OnlyGreedy, b"\x01\x00\x02\x00"),
            (ZeroLength, b"\x05"), (VariableNested, b"\x01\x02\x03")]


@pytest.mark.parametrize("message_class, raw", _shapes(),
                         ids=lambda argument: getattr(argument, "__name__", ""))
def test_edge_shapes_round_trip(message_class, raw):
    from core.IRS.buffers import BinaryReader, BinaryWriter

    message = message_class.from_bytes(BinaryReader(raw))
    assert message.to_bytes() == raw
    writer = BinaryWriter()
    assert message.to_bytes(writer) is None
    assert bytes(writer) == raw


def test_greedy_array_refuses_a_trailing_partial_element():
    """The interpreted loop overruns and raises; the compiled one must too,
    rather than silently dropping the incomplete tail element."""
    import struct

    from core.IRS import Message, UInt16
    from core.IRS.buffers import BinaryReader

    class Greedy(Message):
        data: list[int] = [UInt16, None]

    assert Greedy.from_bytes(BinaryReader(b"\x01\x00\x02\x00")).data == [1, 2]
    with pytest.raises(struct.error):
        Greedy.from_bytes(BinaryReader(b"\x01\x00\x02"))


def test_a_layout_it_cannot_express_falls_back_instead_of_breaking():
    """An array counted by a field parsed AFTER it. The interpreted path raises
    AttributeError; the generator has no local to read, so it must decline the
    class rather than emit something that raises NameError."""
    from core.IRS import Byte, Message, _compiler
    from core.IRS.buffers import BinaryReader

    class CountAfterArray(Message):
        items: list[int] = [Byte, "n"]
        n: int = Byte

    # The observable behaviour is the same either way -- that is the point.
    with pytest.raises(AttributeError):
        CountAfterArray.from_bytes(BinaryReader(b"\x02\x01"))
    if _compiler.ENABLED:
        assert CountAfterArray.__dict__.get("_irs_uncompilable_") is True


def test_generated_source_is_readable():
    """`dump_source` is the debugging surface for a layout that misbehaves."""
    from core.IRS import dump_source
    from core.IRS.benchmark.bench_message_parsing import BenchMessage

    source = dump_source(BenchMessage)
    assert "def from_bytes(cls, reader, instance=None):" in source
    assert "def to_bytes(self, writer=None, value=None):" in source


if __name__ == "__main__":                    # the subprocess entry point
    probe()
