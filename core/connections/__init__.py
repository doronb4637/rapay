"""
connection_framework
=====================

JSON-configured TCP, UDP, Multicast and RTI Connext DDS connections: asyncio
does the I/O, callers get a plain synchronous API. Build connections through
`ConnectionManager`; payloads are encoded/decoded by the project's IRS package.

See README.md for the full design write-up.
"""
import logging

from core.annotations import IrsMessage, OpCode

from ._routes import ConnectCallback, ReceiveCallback
from .config import ConnectionConfig, Side, TransportProtocol
from .framing import IRSDataError
from .base import Connection, ConnectedTarget, Unit
from .composite import CompositeUnit
from .handlers import UnitHandler, on_connect, route
from .manager import ConnectionManager

from .tcp import TcpConnection
from .udp import UdpConnection
from .multicast import MulticastConnection

ConnectionManager.register(TransportProtocol.TCP, TcpConnection)
ConnectionManager.register(TransportProtocol.UDP, UdpConnection)
ConnectionManager.register(TransportProtocol.MULTICAST, MulticastConnection)
# DDS requires the RTI Connext Python API; any other import error is a real bug.
try:
    from .dds import DdsConnection
    ConnectionManager.register(TransportProtocol.DDS, DdsConnection)
except ModuleNotFoundError as exc:
    if (exc.name or "").split(".")[0] != "rti":
        raise
    logging.getLogger("connmgr").warning("rti.connextdds is not installed: DDS connections are unavailable")
    DdsConnection = None

__all__ = [
    "ConnectionManager", "ConnectionConfig", "TransportProtocol", "Side",
    "Connection", "Unit", "CompositeUnit", "ConnectedTarget",
    "TcpConnection", "UdpConnection", "MulticastConnection", "DdsConnection",
    "UnitHandler", "route", "on_connect",
    "IrsMessage", "OpCode", "ReceiveCallback", "ConnectCallback", "IRSDataError",
]
