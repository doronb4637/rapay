from annotations import *


""" GLOBAL MESSAGE REGISTRY """
MESSAGE_REGISTRY: dict[UnitCode, dict[OpCode, IrsMessage]] = {}
PAIR_REGISTRY: dict[UnitCode, UnitCode] = {}

def register_message(unitCode: int, opCode: int, message: IrsMessage) -> None:
    """
    Registers a message parser to the UNIFIED_REGISTRY.
    This single function will be imported and used by all message structure files.
    """
    if unitCode not in MESSAGE_REGISTRY:
        MESSAGE_REGISTRY[unitCode] = {}
    MESSAGE_REGISTRY[unitCode][opCode] = message

def register_pair(unitCode: int, pair: UnitCode) -> None:
    PAIR_REGISTRY[pair] = unitCode
