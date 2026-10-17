"""
Binary message framing utilities.

Every TCP / UDP / Multicast payload is prefixed with a fixed header:

    ( UnitCode: uint8, OpCode: uint16, DataLength: uint16 )

packed LITTLE ENDIAN using a pre-compiled struct.Struct (5 bytes total).
DDS payloads are data-centric and never touch this module (see dds.py).
"""

import struct
from dataclasses import dataclass

from core.IRS.irs_parser import IRSDataError

# "<"(little-endian), B(uint8): UnitCode, H(uint16): OpCode, H: Len.
_HEADER_STRUCT = struct.Struct("<BHH")  # Total 5 bytes

HEADER_SIZE = _HEADER_STRUCT.size


@dataclass(frozen=True, slots=True)
class MessageHeader:
    """The decoded fixed header of one framed message."""

    unit_code: int
    opcode: int
    data_length: int


def pack_message(unit_code: int, opcode: int, payload: bytes) -> bytes:
    """
    Build header + payload, ready to write to a socket.

    `unit_code` and `opcode` are trusted to fit a uint8 and a uint16 (config and
    structures files already guarantee it). Raises `IRSDataError` if `payload`
    exceeds the uint16 length field (64KB).
    """
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
    """Parse one complete datagram (UDP/Multicast). Its size must match the
    header exactly: short or long, the frame is corrupt."""
    header = unpack_header(raw)
    payload = raw[HEADER_SIZE:]
    if len(payload) != header.data_length:
        raise IRSDataError(
            f"declared length {header.data_length} but the datagram carries {len(payload)} bytes"
        )
    return header, payload
