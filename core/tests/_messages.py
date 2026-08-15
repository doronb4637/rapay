"""
IRS message layouts owned by the pytest suite, registered once at import
time (`register_message` runs at module scope -- importing this module *is*
the registration, same convention as `IRS.Structures.Test.test_messages`
which `connections/test_framework.py` uses).

Collision with `IRS.Structures.Test.test_messages` is prevented by the
registry itself: layouts are keyed by the module that registered them, so
this file's live under `tests._messages` and are reachable only by a lookup
scoped there (or by an unscoped one, which raises if two modules genuinely
disagree about a route). The distinct unit-code/opcode range (200-219 here,
1-162 there) is now just tidiness, not the mechanism.

Deliberately NOT `from __future__ import annotations`: `IRS.core.MessageMeta`
reads `__annotations__` at class-body execution time and hands the real type
objects to beartype (`is_bearable`) on every attribute set -- stringified
(PEP 563) annotations break that (beartype can't resolve a bare string
forward-ref here), which is exactly what `IRS.Structures.Test.test_messages`
also avoids by omitting the future import.
"""
from enum import IntEnum

from IRS import Byte, Message, UInt16, baseType
from IRS.REGISTRY import register_message

#: Opaque byte payload -- what most transport/dispatch-level tests need: they
#: only care that bytes arrived intact, not what they mean.
class Text(Message):
    data: list[int] = [Byte, None]


#: A small typed message, for the handful of tests that care about IRS
#: actually parsing structured fields (not just passing bytes through).
@baseType(1)
class E_Kind(IntEnum):
    UNKNOWN = 0x00
    A = 0x01
    B = 0x02


class Ping(Message):
    """5 bytes on the wire: uint16, uint8, uint16."""
    seq: int = UInt16
    kind: E_Kind
    value: int = UInt16


#: unitCode 200's peer talks Text on every opcode in this range -- covers
#: every generic transport/dispatch/handler test.
TEXT_UNIT_CODE = 200
TEXT_OPCODES = range(1, 20)
for _opcode in TEXT_OPCODES:
    register_message(unitCode=TEXT_UNIT_CODE, opCode=_opcode, message=Text)

#: A second Text-speaking unit code, for tests that need two distinct peers
#: (e.g. multi-unit routing, unit-code collisions) without reusing 200.
TEXT_UNIT_CODE_2 = 201
for _opcode in TEXT_OPCODES:
    register_message(unitCode=TEXT_UNIT_CODE_2, opCode=_opcode, message=Text)

#: Typed round-trip: unitCode 210's peer sends Ping on opcode 21.
PING_UNIT_CODE = 210
PING_OPCODE = 21
register_message(unitCode=PING_UNIT_CODE, opCode=PING_OPCODE, message=Ping)
