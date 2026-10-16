"""
The pytest suite's own structures file: its units and their `@Definition`
layouts. Every test connection names it in `Structures` (as `MESSAGES`), so
every unit name a test config uses must be one of the unit constants below,
with the same code -- `config.validate_interfaces` checks exactly that.

Deliberately NOT `from __future__ import annotations`: `IRS.core.MessageMeta`
reads `__annotations__` at class-body execution time and hands the real type
objects to beartype (`is_bearable`) on every attribute set -- stringified
(PEP 563) annotations break that.
"""
from enum import IntEnum

from core.IRS import Byte, Definition, Message, UInt16, baseType

#: How test configs name this file.
MESSAGES = __name__

#: Codes test connections stamp as their OWN (`"unitCode"` at the top level).
Local = 100
Local2 = 101
Local3 = 102

#: unitCode 200's peer talks Text on every opcode in TEXT_OPCODES -- covers
#: every generic transport/dispatch/handler test. Each name is one way a test
#: config calls that peer.
TEXT_UNIT_CODE = Peer = Peer0 = PeerA = Radar = A = Beacon = silent = unit1 = 200
#: A second Text-speaking unit, for tests that need two distinct peers.
TEXT_UNIT_CODE_2 = Peer1 = Peer2 = PeerB = Tracker = B = alive = unit2 = 201
#: Typed round-trip: unit 210 sends Ping on opcode 21.
PING_UNIT_CODE = Server = 210
PING_OPCODE = 21

TEXT_OPCODES = range(1, 20)


#: Opaque byte payload -- what most transport/dispatch-level tests need: they
#: only care that bytes arrived intact, not what they mean. Undecorated; each
#: route gets its own `@Definition`'d subclass below.
class Text(Message):
    data: list[int] = [Byte, None]


def _text_route(unit_code: int, opcode: int) -> type[Message]:
    """One Text layout per route: a class carries exactly one (unitCode, opCode)."""
    name = f"Text_{unit_code}_{opcode}"
    cls = type(name, (Text,), {
        "__module__": __name__, "__qualname__": name,
        "__annotations__": {"data": list[int]}, "data": [Byte, None],
    })
    return Definition(unitCode=unit_code, opCode=opcode)(cls)


for _unit_code in (TEXT_UNIT_CODE, TEXT_UNIT_CODE_2):
    for _opcode in TEXT_OPCODES:
        _cls = _text_route(_unit_code, _opcode)
        globals()[_cls.__name__] = _cls


#: A small typed message, for the handful of tests that care about IRS
#: actually parsing structured fields (not just passing bytes through).
@baseType(1)
class E_Kind(IntEnum):
    UNKNOWN = 0x00
    A = 0x01
    B = 0x02


@Definition(unitCode=PING_UNIT_CODE, opCode=PING_OPCODE)
class Ping(Message):
    """5 bytes on the wire: uint16, uint8, uint16."""
    seq: int = UInt16
    kind: E_Kind
    value: int = UInt16
