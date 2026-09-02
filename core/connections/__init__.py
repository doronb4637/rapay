"""
connection_framework
=====================

A protocol-agnostic connection management system for TCP, UDP, Multicast and
RTI Connext DDS, driven by JSON configuration, with asyncio doing the I/O
under the hood and a plain synchronous API on top.

Message payloads themselves are not this package's business: `irs_to_bytes` /
`parse_irs` come from the project's `IRS` package and are re-exported here so
`from connections import parse_irs` keeps working, and config/opcode coercion
plus config-file loading come from `tools`.

See README.md for the full design write-up.
"""

from core.IRS.irs_parser import irs_to_bytes, parse_irs

from .config import ConnectionConfig, Protocol, Side
from .framing import (
    HEADER_SIZE,
    MessageHeader,
    IRSDataError,
    pack_message,
    unpack_message,
    unpack_header,
)
from .base import Connection, ConnectedTarget, IrsMessage, Unit, get_event_loop_thread
from .composite import CompositeUnit
from .handlers import UnitHandler, on_connect, route
from .manager import ConnectionManager

from .tcp import TcpConnection
from .udp import UdpConnection
from .multicast import MulticastConnection

# DDS requires the RTI Connext Python API. We register it opportunistically:
# the rest of the framework works fine without RTI installed.
try:
    from .dds import DdsConnection
    ConnectionManager.register(Protocol.DDS, DdsConnection)
except ImportError:
    print(f"[!]: rti.connext module is not installed,"
          f"DDS connections will not work")
    DdsConnection = None  # type: ignore

ConnectionManager.register(Protocol.TCP, TcpConnection)
ConnectionManager.register(Protocol.UDP, UdpConnection)
ConnectionManager.register(Protocol.MULTICAST, MulticastConnection)

__all__ = [
    "ConnectionConfig", "Protocol", "Side",
    "HEADER_SIZE", "MessageHeader", "IRSDataError",
    "pack_message", "unpack_message", "unpack_header",
    "Connection", "Unit", "get_event_loop_thread",
    "ConnectedTarget", "IrsMessage", "irs_to_bytes", "parse_irs",
    "CompositeUnit", "UnitHandler", "route", "on_connect", "ConnectionManager",
    "TcpConnection", "UdpConnection", "MulticastConnection", "DdsConnection",
]
