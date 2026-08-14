"""
connections/framing.py -- pure pack/unpack, no I/O. Header is
(UnitCode: uint8, OpCode: uint16, DataLength: uint16), little-endian, 5 bytes.
"""
import pytest

from connections.framing import (
    HEADER_SIZE,
    MessageHeader,
    IRSDataError,
    pack_message,
    unpack_header,
    unpack_message,
)


def test_header_size_is_five_bytes():
    assert HEADER_SIZE == 5


def test_pack_then_unpack_round_trip():
    frame = pack_message(unit_code=7, opcode=0x1234, payload=b"hello")
    header, payload = unpack_message(frame)
    assert header == MessageHeader(unit_code=7, opcode=0x1234, data_length=5)
    assert payload == b"hello"


def test_pack_empty_payload():
    frame = pack_message(unit_code=1, opcode=1, payload=b"")
    header, payload = unpack_message(frame)
    assert header.data_length == 0
    assert payload == b""


@pytest.mark.parametrize("unit_code", [-1, 256, 1000])
def test_pack_rejects_unit_code_out_of_uint8_range(unit_code):
    with pytest.raises(IRSDataError, match="does not fit in a byte"):
        pack_message(unit_code=unit_code, opcode=1, payload=b"")


@pytest.mark.parametrize("opcode", [-1, 0x10000])
def test_pack_rejects_opcode_out_of_uint16_range(opcode):
    with pytest.raises(IRSDataError, match="does not fit in uint16"):
        pack_message(unit_code=1, opcode=opcode, payload=b"")


def test_pack_rejects_payload_over_uint16_length():
    with pytest.raises(IRSDataError, match="64KB"):
        pack_message(unit_code=1, opcode=1, payload=b"x" * 0x10000)


def test_pack_accepts_boundary_values():
    # 0/max at every field is legal, not off-by-one rejected.
    frame = pack_message(unit_code=0xFF, opcode=0xFFFF, payload=b"x" * 0xFFFF)
    header, payload = unpack_message(frame)
    assert header.unit_code == 0xFF
    assert header.opcode == 0xFFFF
    assert len(payload) == 0xFFFF


def test_unpack_header_rejects_short_buffer():
    with pytest.raises(IRSDataError, match="need 5 bytes"):
        unpack_header(b"\x01\x02\x03")


def test_unpack_message_rejects_truncated_payload():
    """DataLength claims more than is actually present -- the TCP read loop
    never produces this (it reads exactly `data_length` bytes before
    dispatching), but UDP/Multicast hand `unpack_message` a single received
    datagram that could be short/corrupt on the wire."""
    frame = pack_message(unit_code=1, opcode=1, payload=b"hello")
    truncated = frame[:-2]  # header claims 5 bytes, only 3 are present
    with pytest.raises(IRSDataError, match="declared length"):
        unpack_message(truncated)


def test_unpack_message_ignores_trailing_garbage_after_declared_length():
    """A well-formed datagram followed by extra bytes still parses -- only
    the declared length is sliced off; nothing downstream reads past it."""
    frame = pack_message(unit_code=1, opcode=1, payload=b"hello") + b"EXTRA"
    header, payload = unpack_message(frame)
    assert payload == b"hello"
