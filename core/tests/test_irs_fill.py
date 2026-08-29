"""
`fill()` and the codec edges around it.

`fill()` had no callers and no coverage -- `grep -rn "\\.fill()" core` found only its
own internal recursion -- which is why a first real consumer (GSim, building form
payloads) turned up this many faults in it at once. Each test below pins one of them
and is named for the behaviour, not the bug.

The through-line: **`fill()` owns absence.** A field nobody set gets a safe default at
any depth, that default is a fresh object rather than a shared one, and an enum with no
0 member is left explicitly *unset* (`None`) rather than guessed at -- with `None`
travelling intact through `to_dict`/`from_dict` and reaching the wire as 0.

Deliberately NOT `from __future__ import annotations`, for the reason spelled out in
`core/tests/_messages.py`: `MessageMeta` reads `__annotations__` at class-body execution
time and hands the real type objects to beartype.
"""
import logging
from enum import IntEnum

import pytest

from core.IRS import BitField, Byte, Message, Single, Structure, baseType
from core.IRS.buffers import BinaryReader


@baseType(1)
class E_Zero(IntEnum):
    OFF = 0
    ON = 1


@baseType(1)
class E_NoZero(IntEnum):
    """No 0 member -- what `fill()` has nothing to choose, and answers `None`."""
    A = 1
    B = 2


@baseType(1)
class Bits(BitField):
    mode: E_NoZero = 2
    pad: int = 6


class Inner(Structure):
    a: int = Byte
    b: int = Byte


class FixedStructs(Message):
    items: tuple[Inner] = [Inner, 3]


class Nested(Message):
    inner: Inner
    tail: int = Byte


class UnsetEnum(Message):
    e: E_NoZero


class WithBits(Message):
    flags: Bits


class Floaty(Message):
    f: float = Single


class Flagged(Message):
    flag: E_Zero
    value: int = Byte


# --------------------------------------------------------------------------- #
# Fresh objects, not shared ones
# --------------------------------------------------------------------------- #
def test_fixed_array_slots_are_independent():
    """Each slot is its own object, so editing one leaves the rest alone."""
    message = FixedStructs.from_dict({}).fill()
    message.items[0].a = 99

    assert [item.a for item in message.items] == [99, 0, 0]
    assert message.items[0] is not message.items[1]


def test_filling_one_message_cannot_affect_the_next():
    """`ArrayField.baseType` is a CLASS-level object. Handing it out as an array
    element made one message's edit visible in every message built afterwards --
    process-wide contamination that outlived the request that caused it."""
    first = FixedStructs.from_dict({}).fill()
    first.items[0].a = 99

    assert FixedStructs.from_dict({}).fill().to_bytes() == b"\x00" * 6
    assert first.items[0] is not FixedStructs._fields_[0].baseType


def test_nested_struct_is_not_the_shared_field_object():
    message = Nested.from_dict({}).fill()
    assert message.inner is not Nested._fields_[0]


# --------------------------------------------------------------------------- #
# Absence at depth
# --------------------------------------------------------------------------- #
def test_fill_completes_a_partial_nested_struct():
    """`hasattr` answers "is this slot set", not "is this subtree complete": the
    struct is present, its own `b` is not, and `to_bytes` used to raise a bare
    AttributeError from two levels down."""
    message = Nested.from_dict({"inner": {"a": 7}}).fill()

    assert message.inner.b == 0
    assert message.to_bytes() == b"\x07\x00\x00"


def test_fill_completes_partial_array_items():
    message = FixedStructs.from_dict({"items": [{"a": 1}, {"a": 2}, {"a": 3}]}).fill()
    assert message.to_bytes() == b"\x01\x00\x02\x00\x03\x00"


def test_fill_leaves_values_that_are_already_set():
    message = Nested.from_dict({"inner": {"a": 7, "b": 8}, "tail": 9}).fill()
    assert message.to_bytes() == b"\x07\x08\x09"


def test_float_fills_as_float():
    assert Floaty.from_dict({}).fill().f == 0.0


# --------------------------------------------------------------------------- #
# The unset enum
# --------------------------------------------------------------------------- #
def test_enum_with_a_zero_member_fills_to_it():
    assert Flagged.from_dict({}).fill().flag is E_Zero.OFF


def test_unset_enum_survives_the_whole_round_trip():
    """`None` is the deliberate "unset" for an enum with no 0 member. `fill()` was
    the only method that produced it and every other one rejected it, so a filled
    message simply could not be encoded."""
    message = UnsetEnum.from_dict({}).fill()
    assert message.e is None

    as_dict = message.to_dict()
    assert as_dict == {"e": None}

    assert UnsetEnum.from_dict(as_dict).fill().to_bytes() == b"\x00"


def test_unset_enum_reaches_the_wire_as_zero():
    assert UnsetEnum.from_dict({}).fill().to_bytes() == b"\x00"


def test_unset_enum_decodes_back_from_the_wire():
    """The other half of "unset is written as 0": without it an unset enum could
    be sent and never received -- `from_bytes` raised, so the whole message was
    dropped, with no error row anywhere in GSim to say why."""
    assert UnsetEnum.from_bytes(BinaryReader(b"\x00")).e is None


def test_undefined_enum_value_from_the_wire_is_rejected():
    """A non-zero value no member defines is not an unset, it is a sender putting
    data on the wire that contradicts the shared specification. Raising drops and
    logs the message (`Connection._decode` wraps this in IRSDataError)."""
    with pytest.raises(ValueError, match="7 is not a defined member"):
        UnsetEnum.from_bytes(BinaryReader(b"\x07"))


def test_to_dict_resolves_a_raw_value_to_its_member():
    """Stringifying it gave `'2'`, and `from_dict('2')` then looked up a member
    NAMED "2" -- so `to_dict` broke its own round trip."""
    field = Flagged._fields_[0]
    assert field.to_dict(1) == "ON"
    assert field.from_dict(field.to_dict(1)) is E_Zero.ON


# --------------------------------------------------------------------------- #
# Bitfields
# --------------------------------------------------------------------------- #
def test_unset_bit_reads_as_none_without_warning(caplog):
    """0 on an enum with no 0 member is the deliberate unset, so it stays quiet."""
    with caplog.at_level(logging.WARNING):
        assert Bits(0).mode is None
        assert Bits(0).to_dict() == {"mode": None, "pad": 0}
    assert caplog.records == []


def test_undefined_bit_reads_as_none_and_warns_once(caplog):
    """A 2-bit field can hold 3 with only {1, 2} defined -- wire data contradicting
    the spec. Readable (a decode must not break) but never silent."""
    from core.IRS import bitfields
    bitfields._warned_bits.clear()

    with caplog.at_level(logging.WARNING):
        assert Bits(0b11).mode is None
        Bits(0b11).mode
        Bits(0b11).mode

    # Deduped: the getter runs on every property read, so an unfiltered warning
    # would repeat at the link's full message rate.
    assert len(caplog.records) == 1
    assert "3 is not a defined member" in caplog.records[0].message


def test_partial_bitfield_dict_fills_the_missing_bits():
    """`Structure.from_dict` assigns only the keys it is given; this one demanded
    every bit and raised KeyError before `fill()` could repair anything."""
    message = WithBits.from_dict({"flags": {"mode": 2}}).fill()
    assert message.to_dict() == {"flags": {"mode": "B", "pad": 0}}
    assert message.to_bytes() == b"\x02"


def test_absent_bitfield_fills_to_zero():
    assert WithBits.from_dict({}).fill().to_bytes() == b"\x00"


# --------------------------------------------------------------------------- #
# Loud failures
# --------------------------------------------------------------------------- #
def test_short_fixed_array_is_rejected():
    """`to_bytes` just iterates, so a short list emitted a short frame and every
    field after it mis-parsed on the receiving side, with nothing raised at either
    end."""
    message = FixedStructs.from_dict({"items": [{"a": 1, "b": 2}]}).fill()
    with pytest.raises(ValueError, match="declared as exactly 3"):
        message.to_bytes()


def test_field_without_a_wire_format_is_rejected_at_class_definition():
    """A lost `= Byte` dropped the field from `_fields_` while leaving it in
    `__slots__`, so the class looked normal and quietly stopped carrying it."""
    with pytest.raises(TypeError, match="Typo.oops: annotated <class 'int'> with no Type/Size"):
        class Typo(Message):
            good: int = Byte
            oops: int


def test_enum_attribute_rejects_a_non_member():
    """This check existed but sat behind an `elif` that could never be reached, so
    enum fields were the one kind never validated."""
    message = Flagged.from_dict({}).fill()

    for bad in ("ONN", 99, 3.7, [1, 2]):
        with pytest.raises(TypeError, match=f"Flagged.flag expects a E_Zero member, name, value, or None, got"):
            message.flag = bad


def test_enum_attribute_accepts_every_legal_spelling():
    message = Flagged.from_dict({}).fill()

    for good in (E_Zero.ON, "ON", 1, None):
        message.flag = good        # member, name, raw value, unset

    # The non-enum path is unchanged and still checked.
    with pytest.raises(TypeError):
        message.value = "not an int"
