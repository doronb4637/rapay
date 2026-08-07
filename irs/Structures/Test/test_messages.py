"""
The IRS message set used by connections/test_framework.py.

Deliberately small -- two flat messages, one per direction -- because what the
connection tests have to prove is that the framework hands payloads to IRS and
gets objects back, not that IRS can express complicated layouts.

Both messages share ONE opcode and differ only in which unit sends them. That
is the point: `parse_irs` selects a layout by the SENDER's unit code, so a
TrackAck arriving on opcode 60 can only have been chosen by the server's code
(22) and a TrackReport on the same opcode only by the client's (21).

Importing this module is what registers the layouts -- `register_message` runs
at module level. The test configs name it under "libs_path", so
`ConnectionManager.create()` imports it (via `tools.general.import_modules`)
before the connection object exists.
"""
from enum import IntEnum

from IRS import *
from IRS.REGISTRY import register_message

#: The two peers in test_framework.test_irs_parser_roundtrip.
CLIENT_UNIT_CODE = 21
SERVER_UNIT_CODE = 22
#: One opcode, both directions -- see the module docstring.
TRACK_OPCODE = 60


@baseType(1)
class E_TrackKind(IntEnum):
    UNKNOWN = 0x00
    AIR = 0x01
    SURFACE = 0x02


class TrackReport(Message):
    """Client -> server. 5 bytes on the wire: uint16, uint8, uint16."""
    trackId: int = UInt16
    kind: E_TrackKind
    heading: int = UInt16


register_message(
    unitCode=CLIENT_UNIT_CODE,
    opCode=TRACK_OPCODE,
    message=TrackReport
)


class TrackAck(Message):
    """Server -> client. 3 bytes on the wire: uint16, uint8."""
    trackId: int = UInt16
    accepted: int = Byte


register_message(
    unitCode=SERVER_UNIT_CODE,
    opCode=TRACK_OPCODE,
    message=TrackAck
)
