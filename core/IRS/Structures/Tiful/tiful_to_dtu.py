from IRS import *
from enum import IntEnum
from IRS.REGISTRY import register_message

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


""" Tiful Messages"""
class GeneralFlag(Message):
    Flag: E_Flag
    value: int = Byte


register_message(
    unitCode=0x01,
    opCode=0x0012,
    message=GeneralFlag
)


class ArrayOfAreas(Message):
    Len: int = Byte
    Areas: tuple[S_Area] = [S_Area, "Len"]


register_message(
    unitCode=0x01,
    opCode=0x0001,
    message=ArrayOfAreas
)

""" DTU Messages """
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

class Data(Structure):
    num1: int = Byte
    value: int = Byte
    array: int = [Byte, 9]

class StructMessage(Message):
    data: Data

register_message(
    unitCode=0x01,
    opCode=0x0200,
    message=StructMessage
)
