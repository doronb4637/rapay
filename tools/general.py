import importlib
from annotations import *

def import_modules(libs: list[str] | str):
    if isinstance(libs, str):
        libs = [libs]
    for lib in libs:
        importlib.import_module(lib)


def validated_opCode(opCode: OpCode | str) -> OpCode:
    if isinstance(opCode, int):
        return opCode
    return int(opCode, 0)


def validated_unitCode(unitCode: UnitCode | str) -> UnitCode:
    return validated_opCode(unitCode)
