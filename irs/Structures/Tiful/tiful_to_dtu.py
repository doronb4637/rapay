from irs import *
from enum import IntEnum


@baseType(1)
class E_Flag(IntEnum):
    OFF = 0x00
    INITIALIZING = 0x01
    ON = 0x02


class GeneralFlag(Message):
    Flag: E_Flag
    value: int = Byte


class GetGeneralFlag(Message):
    Flag: E_Flag


class SetGeneralFlag(Message):
    Flag: E_Flag
    value: int = Byte
