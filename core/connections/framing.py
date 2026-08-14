"""
Binary message framing utilities.

Every TCP / UDP / Multicast payload is prefixed with a fixed header:

    ( UnitCode: uint8, OpCode: uint16, DataLength: uint16 )

packed LITTLE ENDIAN using a pre-compiled struct.Struct (5 bytes total).
DDS payloads are data-centric and never touch this module (see dds.py).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from IRS.irs_parser import IRSDataError
# "<" = little-endian, no padding. B = uint8, H = uint16, H = uint16.
_HEADER_STRUCT = struct.Struct("<BHH")

HEADER_SIZE = _HEADER_STRUCT.size      # 5 bytes


@dataclass(frozen=True, slots=True)
class MessageHeader:
    unit_code: int
    opcode: int
    data_length: int


def pack_message(unit_code: int, opcode: int, payload: bytes) -> bytes:
    """Build header + payload ready to write to a socket."""
    if not (0 <= unit_code <= 0xFF):
        raise IRSDataError(f"unit_code {unit_code} does not fit in a byte")
    if not (0 <= opcode <= 0xFFFF):
        raise IRSDataError(f"opcode {opcode} does not fit in uint16")
    if len(payload) > 0xFFFF:
        raise IRSDataError("payload exceeds uint16 length field (64KB)")
    header = _HEADER_STRUCT.pack(unit_code, opcode, len(payload))
    return header + payload


def unpack_header(raw: bytes) -> MessageHeader:
    """Parse just the fixed-size header from the front of `raw`."""
    if len(raw) < HEADER_SIZE:
        raise IRSDataError(f"need {HEADER_SIZE} bytes for header, got {len(raw)}")
    unit_code, opcode, data_length = _HEADER_STRUCT.unpack(raw[:HEADER_SIZE])
    return MessageHeader(unit_code, opcode, data_length)


def unpack_message(raw: bytes) -> tuple[MessageHeader, bytes]:
    """Parse header + payload from a single complete datagram (UDP/Multicast)."""
    header = unpack_header(raw)
    payload = raw[HEADER_SIZE:HEADER_SIZE + header.data_length]
    if len(payload) != header.data_length:
        raise IRSDataError(
            f"declared length {header.data_length} but only {len(payload)} bytes available"
        )
    return header, payload
