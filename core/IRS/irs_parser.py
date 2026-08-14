import logging

from .buffers import BinaryReader
from .REGISTRY import *
from annotations import *

logger = logging.getLogger("parser")

""" Exception's """
class IRSNotFoundError(Exception):
    """Raised when the IRS instance or record is missing."""
    pass


class IRSDataError(Exception):
    """Raised when a header/payload cannot be built or parsed correctly."""


""" Private Helpers"""
def _get_message_class(unitCode: UnitCode, opCode: OpCode) -> IrsMessage | None:
    """Finds the message class.
    Args:
        unitCode (int):Code representing Sending-Unit/Our-Unit.
        opCode (int): The message code.

    Returns:
        IrsMessage: The uninitialized message class,
        or None when the unit message structure exists but the message
        is not found.
    Raises:
        IRSNotFoundError: when the unit message structure doesn't exists.
    """
    connection_messages = MESSAGE_REGISTRY.get(unitCode)
    if connection_messages is None:
        paired_unitCode = PAIR_REGISTRY.get(unitCode)
        connection_messages = MESSAGE_REGISTRY.get(paired_unitCode)
    if connection_messages is None:
        raise IRSNotFoundError(f"Unit: {unitCode}, Was not found! in unit messages")
    return connection_messages.get(opCode)


""" API functions for IRS"""
def validate_irs(unitCode: UnitCode, opCode: OpCode) -> None:
    """Validates the existence of the IRS.
    Raises:
        NotImplementedError: If the IRS does not exist."""
    check_unit_message_structure = _get_message_class(unitCode, opCode)
    if check_unit_message_structure is not None:
        return None
    raise IRSNotFoundError(f"Message of OpCode: {opCode}, Was not Implemented! for Unit: {unitCode}")


def is_irs_exist(unitCode: UnitCode, opCode: OpCode) -> bool:
    """ Returns whether the message is registered or not"""
    try:
        return _get_message_class(unitCode, opCode) is not None
    except IRSNotFoundError:
        return False


def irs_to_bytes(unitCode: UnitCode, opCode: OpCode, message: IrsMessage | dict) -> bytes:
    if isinstance(message, dict):
        message_class = _get_message_class(unitCode, opCode)
        if message_class is not None:
            message = message_class.from_dict(message)
        else:
            raise IRSNotFoundError(f"Message of OpCode: {opCode}, Was not Implemented! for Unit: {unitCode}")
    return message.to_bytes()


def parse_irs(unitCode: UnitCode, opCode: OpCode, payload: bytes) -> tuple[str, IrsMessage] | None:
    message_class = _get_message_class(unitCode, opCode)
    if message_class is not None:
        return message_class.__name__, message_class.from_bytes(BinaryReader(payload))
    raise IRSNotFoundError(f"Message of OpCode: {opCode}, Was not Implemented! for Unit: {unitCode}")
