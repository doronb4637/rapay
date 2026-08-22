"""
Byte order: declared once per structures file, resolved once per class.

IRS was little-endian-only; a specification is now free to say `ENDIAN = bigEndian`
at the top of its file and have every field, enum and bitfield below follow. The
rules these tests pin down:

  * no declaration means little endian, so every pre-existing file is unaffected;
  * `@baseType(n)` inherits the file's declaration, `@baseType(n, endian)` overrides it;
  * a type carries the byte order of the file that DEFINED it, not the file that
    embeds it -- which is what makes "one endian per specification" coherent;
  * resolution happens at class-creation time, never inside `from_bytes`/`to_bytes`.

This module declares no `ENDIAN`, so the layouts defined here ARE the little-endian
control group. Their big-endian twins live in `_messages_big_endian.py` -- a second
file is not incidental, it is the only way to have both byte orders in one test,
since the declaration is per file by design.

Deliberately NOT `from __future__ import annotations`, for the reason spelled out
in `core/tests/_messages.py`.
"""
import sys
from enum import IntEnum

import pytest

from core.IRS import (
    BitField,
    Byte,
    Field,
    Message,
    Structure,
    UInt16,
    UInt32,
    baseType,
    bigEndian,
    littleEndian,
)
from core.IRS.buffers import BinaryReader
from core.IRS.constants import module_endian

from . import _messages_big_endian as be


""" Little-endian control group -- field-for-field identical to `_messages_big_endian` """
@baseType(2)
class E_Kind(IntEnum):
    UNKNOWN = 0x00
    A = 0x01
    B = 0x02


@baseType(2)
class Flags(BitField):
    active: int = 1
    error: int = 1
    mode: int = 2
    reserved: int = 12


class Position(Structure):
    x: int = UInt16
    y: int = UInt32


class Sample(Message):
    seq: int = UInt16
    kind: E_Kind
    flags: Flags
    pos: Position
    count: int = Byte
    values: list[int] = [UInt16, "count"]


#: This module, so `_build` can be handed the little-endian layouts the same way
#: it is handed the big-endian ones.
le = sys.modules[__name__]


def _build(module) -> 'Message':
    """The same values, laid out by whichever module's `Sample` is passed."""
    flags = module.Flags()
    flags.active = 1
    flags.error = 0
    flags.mode = 2

    pos = module.Position()
    pos.x = 0x1234
    pos.y = 0x89ABCDEF

    message = module.Sample()
    message.seq = 0x0102
    message.kind = module.E_Kind.B
    message.flags = flags
    message.pos = pos
    message.count = 3
    message.values = [0x1111, 0x2222, 0x3333]
    return message


""" The declaration itself """
def test_module_without_declaration_is_little_endian():
    """Every structures file predating this feature must keep its exact byte layout."""
    assert module_endian(__name__) == littleEndian
    assert Sample._fields_[0].packer.format == '<H'


def test_module_declaration_reaches_plain_fields():
    assert module_endian(be.__name__) == bigEndian
    assert be.Sample._fields_[0].packer.format == '>H'


@pytest.mark.parametrize("declared", ['!', '=', 'big', '', None])
def test_invalid_declaration_is_rejected(declared, monkeypatch):
    """Fail fast at import, not silently on the wire."""
    monkeypatch.setattr(be, 'ENDIAN', declared, raising=False)
    with pytest.raises(ValueError, match="must be bigEndian"):
        module_endian(be.__name__)


def test_unknown_module_falls_back_to_little_endian():
    """A class built by `exec` has no entry in `sys.modules` -- it must not explode."""
    assert module_endian('no.such.module') == littleEndian
    assert module_endian(None) == littleEndian


""" @baseType inheritance and override """
def test_basetype_inherits_the_file_declaration():
    assert E_Kind._baseType_ == '<H'
    assert be.E_Kind._baseType_ == '>H'
    assert Flags._packer_.format == '<H'
    assert be.Flags._packer_.format == '>H'


def test_explicit_basetype_endian_overrides_the_file():
    """`@baseType(2, littleEndian)` inside the big-endian file still means '<H'."""
    assert be.E_LegacyKind._baseType_ == '<H'


""" Every field kind on the wire """
def test_nested_structure_and_array_follow_the_declaration():
    assert [field.packer.format for field in be.Position._fields_] == ['>H', '>I']
    assert [field.packer.format for field in Position._fields_] == ['<H', '<I']

    be_values = be.Sample._fields_[-1]
    assert be_values.baseType.packer.format == '>H'
    assert Sample._fields_[-1].baseType.packer.format == '<H'


def test_big_endian_message_is_the_byte_reversal_of_the_little_endian_one():
    little_bytes = _build(le).to_bytes()
    big_bytes = _build(be).to_bytes()

    assert len(little_bytes) == len(big_bytes)
    assert little_bytes != big_bytes

    # seq(H) kind(H) flags(H) pos.x(H) pos.y(I) count(B) values(3H)
    widths = (2, 2, 2, 2, 4, 1, 2, 2, 2)
    assert sum(widths) == len(big_bytes)
    offset = 0
    for width in widths:
        little_chunk = little_bytes[offset:offset + width]
        assert big_bytes[offset:offset + width] == little_chunk[::-1]
        offset += width


def test_big_endian_round_trip():
    original = _build(be)
    parsed = be.Sample.from_bytes(BinaryReader(original.to_bytes()))

    assert parsed.seq == 0x0102
    assert parsed.kind is be.E_Kind.B
    assert parsed.flags.active == 1 and parsed.flags.error == 0 and parsed.flags.mode == 2
    assert parsed.pos.x == 0x1234
    assert parsed.pos.y == 0x89ABCDEF
    assert parsed.count == 3
    assert parsed.values == [0x1111, 0x2222, 0x3333]


""" The cache still keys on the full format, prefix included """
def test_packers_are_shared_per_format_but_never_across_byte_orders():
    assert Field(UInt16, littleEndian).packer is not Field(UInt16, bigEndian).packer
    assert Field(UInt16, bigEndian).packer is Field(UInt16, bigEndian).packer
    assert be.Sample._fields_[0].packer is be.Position._fields_[0].packer
