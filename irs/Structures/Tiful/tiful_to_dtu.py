from IRS import *
from enum import IntEnum
from IRS.REGISTRY import register_message

@baseType(1)
class E_Flag(IntEnum):
    OFF = 0x00
    INITIALIZING = 0x01
    ON = 0x02


class GeneralFlag(Message):
    Flag: E_Flag
    value: int = Byte


register_message(
    unitCode=0x01,
    opCode=0x0012,
    message=GeneralFlag
)


class GetGeneralFlag(Message):
    Flag: E_Flag


register_message(
    unitCode=0x02,
    opCode=0x0011,
    message=GetGeneralFlag
)


class SetGeneralFlag(Message):
    Flag: E_Flag
    value: int = Byte


register_message(
    unitCode=0x02,
    opCode=0x0012,
    message=SetGeneralFlag
)
