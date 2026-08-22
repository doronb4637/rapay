"""
Array parsing: the bulk-unpack path and the per-element path it replaced.

An array whose element type is a plain primitive is now ONE `struct` operation --
`'<8H'` unpacked in a single call -- instead of one `Field.from_bytes` call per
element. That was the single largest cost in the engine (a 32-byte greedy tail cost
~130 Python calls); it is also the one place where a wrong length silently produces
plausible-looking garbage, so the edges get pinned here rather than assumed.

Arrays of `Structure`, `BitField`, or `IntEnum` deliberately keep the old
element-by-element loop, and tests below prove that path is still live.

Deliberately NOT `from __future__ import annotations`, for the reason spelled out
in `core/tests/_messages.py`.
"""
from enum import IntEnum

import pytest

from core.IRS import (
    BitField,
    Byte,
    Message,
    Structure,
    UInt16,
    UInt32,
    baseType,
)
from core.IRS.buffers import BinaryReader, get_packer

from . import _messages_big_endian as be


""" Little-endian mirror of `_messages_big_endian.Arrays` """
class Arrays(Message):
    fixed: list[int] = [UInt16, 4]
    count: int = Byte
    dynamic: list[int] = [UInt32, "count"]
    greedy: list[int] = [UInt16, None]


@baseType(1)
class E_Kind(IntEnum):
    A = 0x01
    B = 0x02


@baseType(1)
class Flags(BitField):
    on: int = 1
    rest: int = 7


class Pair(Structure):
    a: int = Byte
    b: int = UInt16


class NonPrimitiveArrays(Message):
    """None of these can be bulk-unpacked -- they must still go element by element."""
    kinds: list[E_Kind] = [E_Kind, 3]
    flags: list[Flags] = [Flags, 2]
    pairs: list[Pair] = [Pair, 2]


def _fill(message, fixed=(1, 2, 3, 4), dynamic=(10, 20), greedy=(0xAAAA, 0xBBBB)):
    message.fixed = list(fixed)
    message.count = len(dynamic)
    message.dynamic = list(dynamic)
    message.greedy = list(greedy)
    return message


""" All three shapes round-trip, under both byte orders """
@pytest.mark.parametrize("module_arrays", [Arrays, be.Arrays], ids=["little", "big"])
def test_every_array_shape_round_trips(module_arrays):
    raw = _fill(module_arrays()).to_bytes()
    parsed = module_arrays.from_bytes(BinaryReader(raw))

    assert parsed.fixed == [1, 2, 3, 4]
    assert parsed.count == 2
    assert parsed.dynamic == [10, 20]
    assert parsed.greedy == [0xAAAA, 0xBBBB]
    assert parsed.to_bytes() == raw


def test_byte_order_actually_differs():
    """Guards against the repeated format losing its endian prefix -- the values
    would still round-trip within one byte order while being wrong on the wire."""
    little = _fill(Arrays()).to_bytes()
    big = _fill(be.Arrays()).to_bytes()
    assert len(little) == len(big)
    assert little != big
    assert little[0:2] == big[0:2][::-1]      # fixed[0], a uint16
    assert little[9:13] == big[9:13][::-1]    # dynamic[0], a uint32


""" Degenerate lengths """
def test_zero_length_dynamic_and_greedy():
    message = _fill(Arrays(), dynamic=(), greedy=())
    raw = message.to_bytes()
    parsed = Arrays.from_bytes(BinaryReader(raw))

    assert parsed.count == 0
    assert parsed.dynamic == []
    assert parsed.greedy == []
    assert parsed.to_bytes() == raw


def test_greedy_array_consumes_a_long_tail():
    values = list(range(500))
    raw = _fill(Arrays(), greedy=values).to_bytes()
    assert Arrays.from_bytes(BinaryReader(raw)).greedy == values


""" Failures are explicit, not struct.error from somewhere deep """
def test_greedy_array_rejects_a_partial_trailing_item():
    raw = _fill(Arrays()).to_bytes() + b'\x01'   # one stray byte, greedy items are 2 bytes
    with pytest.raises(ValueError, match=r"not a whole number of 2-byte items"):
        Arrays.from_bytes(BinaryReader(raw))


def test_fixed_array_rejects_a_wrong_length_on_write():
    message = _fill(Arrays(), fixed=(1, 2, 3))
    with pytest.raises(ValueError, match=r"fixed array of 4 items, got 3"):
        message.to_bytes()


""" The per-element path is still there for everything that is not a primitive """
def test_non_primitive_arrays_still_parse_element_by_element():
    message = NonPrimitiveArrays()
    message.kinds = [E_Kind.A, E_Kind.B, E_Kind.A]
    message.flags = [Flags(0), Flags(1)]
    message.pairs = [Pair(a=1, b=2), Pair(a=3, b=4)]
    raw = message.to_bytes()

    for field in NonPrimitiveArrays._fields_:
        assert field._bulk is False, f"{field._name} must not take the bulk path"

    parsed = NonPrimitiveArrays.from_bytes(BinaryReader(raw))
    assert parsed.kinds == [E_Kind.A, E_Kind.B, E_Kind.A]
    assert [f.on for f in parsed.flags] == [0, 1]
    assert [(p.a, p.b) for p in parsed.pairs] == [(1, 2), (3, 4)]
    assert parsed.to_bytes() == raw


""" Memory: a data-driven length must never accumulate packers """
def test_dynamic_lengths_do_not_grow_the_global_packer_cache():
    """`get_packer` is `lru_cache(maxsize=None)`. Routing runtime array lengths through
    it would retain one Struct per distinct length ever received -- the unbounded
    duplication that cache exists to prevent. Dynamic arrays build their own instead."""
    Arrays.from_bytes(BinaryReader(_fill(Arrays()).to_bytes()))   # warm every fixed format
    before = get_packer.cache_info().currsize

    for n in range(1, 60):
        raw = _fill(Arrays(), dynamic=range(n), greedy=range(n)).to_bytes()
        parsed = Arrays.from_bytes(BinaryReader(raw))
        assert parsed.dynamic == list(range(n))
        assert parsed.greedy == list(range(n))

    assert get_packer.cache_info().currsize == before


def test_fixed_arrays_of_the_same_shape_share_one_packer():
    """A fixed format IS bounded by the declarations, so sharing is the right call."""
    class OtherFixed(Message):
        values: list[int] = [UInt16, 4]

    assert Arrays._fields_[0]._cached[1] is OtherFixed._fields_[0]._cached[1]
