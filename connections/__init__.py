"""
connection_framework
=====================

A protocol-agnostic connection management system for TCP, UDP, Multicast and
RTI Connext DDS, driven by JSON configuration, with asyncio doing the I/O
under the hood and a plain synchronous API on top.

See README.md for the full design write-up.
"""

from .config import ConnectionConfig, Protocol, Side
from .framing import (
    HEADER_FORMAT,
    HEADER_SIZE,
    MessageHeader,
    IRSDataError,
    pack_message,
    unpack_message,
    unpack_header,
)
from .base import Connection, get_event_loop_thread
from .composite import CompositeUnit
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
    DdsConnection = None  # type: ignore

ConnectionManager.register(Protocol.TCP, TcpConnection)
ConnectionManager.register(Protocol.UDP, UdpConnection)
ConnectionManager.register(Protocol.MULTICAST, MulticastConnection)

__all__ = [
    "ConnectionConfig", "Protocol", "Side",
    "HEADER_FORMAT", "HEADER_SIZE", "MessageHeader", "IRSDataError",
    "pack_message", "unpack_message", "unpack_header",
    "Connection", "get_event_loop_thread",
    "CompositeUnit", "ConnectionManager",
    "TcpConnection", "UdpConnection", "MulticastConnection", "DdsConnection",
]
