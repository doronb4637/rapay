from IRS import *
from enum import IntEnum

Tiful = 0x01
DTU = 0x02
MFD = 0X03

""" Data Types """
@baseType(1)
class E_Flag(IntEnum):
    OFF = 0x00
    INITIALIZING = 0x01
    ON = 0x02


""" Tiful Messages"""
@Definition(unitCode=Tiful, opCode=0x0012)
class GeneralFlag(Message):
    Flag: E_Flag
    value: int = Byte


""" DTU Messages """
@Definition(unitCode=DTU, opCode=0x0011)
class GetGeneralFlag(Message):
    Flag: E_Flag


@Definition(unitCode=DTU, opCode=0x0012)
class SetGeneralFlag(Message):
    Flag: E_Flag
    value: int = Byte
