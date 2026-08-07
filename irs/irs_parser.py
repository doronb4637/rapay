import logging

from .buffers import BinaryReader
from .REGISTRY import *
from annotations import *

logger = logging.getLogger("parser")


def _get_message_class(unitCode: int, opCode: int) -> IrsMessage:
    connection_messages = MESSAGE_REGISTRY.get(unitCode)
    if connection_messages is None:
        paired_unitCode = PAIR_REGISTRY.get(unitCode)
        connection_messages = MESSAGE_REGISTRY.get(paired_unitCode)
    if connection_messages is None:
        raise NotImplementedError(f"Message: (Unit: {UnitCode}, Code: {OpCode}), Was not found!")
    return connection_messages.get(opCode)


def irs_to_bytes(unitCode: UnitCode, opCode: OpCode, message: IrsMessage | dict) -> bytes:
    if isinstance(message, dict):
        message = _get_message_class(unitCode, opCode).from_dict(message)
    return message.to_bytes()


def parse_irs(unitCode: UnitCode, opCode: OpCode, payload: bytes) -> tuple[str, IrsMessage] | None:
    message_class = _get_message_class(unitCode, opCode)
    return message_class.__name__, message_class.from_bytes(BinaryReader(payload))
