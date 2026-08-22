"""
The big-endian twin of the layouts in `test_endianness.py`.

Endianness in IRS is declared once per FILE (`ENDIAN = bigEndian` below), because
one structures file describes one link and that link does not mix byte orders.
Testing it therefore needs a second module -- there is deliberately no way to put
a big-endian and a little-endian message side by side in the same file.

Every class here is field-for-field identical to its counterpart in
`test_endianness.py`, which declares no `ENDIAN` and so stays little endian. The
two exist only to be compared byte for byte.

Deliberately NOT `from __future__ import annotations`, for the reason spelled out
in `core/tests/_messages.py`.
"""
from enum import IntEnum

from core.IRS import (
    BitField,
    Byte,
    Message,
    Structure,
    UInt16,
    UInt32,
    baseType,
    bigEndian,
    littleEndian,
)

#: The whole file is big endian -- read by `IRS.constants.module_endian` at
#: class-creation time, once per class, never during parsing.
ENDIAN = bigEndian


@baseType(2)
class E_Kind(IntEnum):
    """Inherits `ENDIAN` -> `_baseType_ == '>H'`."""
    UNKNOWN = 0x00
    A = 0x01
    B = 0x02


@baseType(2, littleEndian)
class E_LegacyKind(IntEnum):
    """An explicit endian still wins over the file's declaration -> '<H'."""
    UNKNOWN = 0x00
    A = 0x01
    B = 0x02


@baseType(2)
class Flags(BitField):
    """Inherits `ENDIAN` -> `_packer_.format == '>H'`."""
    active: int = 1
    error: int = 1
    mode: int = 2
    reserved: int = 12


class Position(Structure):
    x: int = UInt16
    y: int = UInt32


class Sample(Message):
    """Exercises every field kind that carries a byte order."""
    seq: int = UInt16
    kind: E_Kind
    flags: Flags
    pos: Position
    count: int = Byte
    values: list[int] = [UInt16, "count"]


class Arrays(Message):
    """All three array shapes, big endian -- the bulk-unpack path builds its own
    repeated format ('>4H'), so each shape needs proving under both byte orders."""
    fixed: list[int] = [UInt16, 4]
    count: int = Byte
    dynamic: list[int] = [UInt32, "count"]
    greedy: list[int] = [UInt16, None]
