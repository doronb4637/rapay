from IRS import *
from enum import IntEnum


from dataclasses import dataclass

#: 0x03 (MFD) has no layouts of its own: it speaks 0x02's (DTU's) IRS.
PAIRS = {0x03: 0x02}

""" Data Types """
@baseType(1)
class E_Flag(IntEnum):
    OFF = 0x00
    INITIALIZING = 0x01
    ON = 0x02


@baseType(1)
class E_Boolean(IntEnum):
    FALSE = 0x00
    TRUE = 0x01


@baseType(1)
class E_Mode(IntEnum):
    FALSE = 0x00
    TRUE = 0x01
    TEST = 0x02


@baseType(1)
class S_Area(BitField):
    fr: E_Mode = 2
    fl: E_Mode = 2
    br: E_Mode = 2
    bl: E_Mode = 2


class Data(Structure):
    num1: int = Byte
    value: int = Byte
    array: tuple[int] = [Byte, 9]


""" Tiful Messages"""
@Definition(unitCode=0x01, opCode=0x0012)
class GeneralFlag(Message):
    Flag: E_Flag
    value: int = Byte


@Definition(unitCode=0x01, opCode=0x0001)
class ArrayOfAreas(Message):
    Len: int = Byte
    Areas: list[S_Area] = [S_Area, "Len"]


@Definition(unitCode=0x01, opCode=0x0200)
class StructMessage(Message):
    datas: tuple[Data] = [Data, 9]


""" DTU Messages """
@Definition(unitCode=0x02, opCode=0x0011)
class GetGeneralFlag(Message):
    Flag: E_Flag


@Definition(unitCode=0x02, opCode=0x0012)
class SetGeneralFlag(Message):
    Flag: E_Flag
    value: int = Byte
