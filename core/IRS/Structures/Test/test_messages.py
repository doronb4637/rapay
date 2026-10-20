"""
A small two-unit IRS used by the test suite's structures-loading tests.

Both messages share ONE opcode and differ only in which unit sends them. That
is the point: a link looks a layout up by the SENDER's unit code, so a TrackAck
arriving on opcode 60 can only have come from the server (22) and a TrackReport
on the same opcode only from the client (21).
"""
from enum import IntEnum

from IRS import *

#: One opcode, both directions -- see the module docstring.
TRACK_OPCODE = 60


@baseType(1)
class E_TrackKind(IntEnum):
    UNKNOWN = 0x00
    AIR = 0x01
    SURFACE = 0x02


@Definition(unitCode=21, opCode=TRACK_OPCODE)
class TrackReport(Message):
    """Client -> server. 5 bytes on the wire: uint16, uint8, uint16."""
    trackId: int = UInt16
    kind: E_TrackKind
    heading: int = UInt16


@Definition(unitCode=22, opCode=TRACK_OPCODE)
class TrackAck(Message):
    """Server -> client. 3 bytes on the wire: uint16, uint8."""
    trackId: int = UInt16
    accepted: int = Byte
