"""
Multicast connection: UDP datagrams plus IP_ADD_MEMBERSHIP group joins.

Direction (send-only vs receive-only vs duplex) is derived ENTIRELY from
`config.side` -- there is no separate "mode"/"duplex" flag in config.extra
for this class:

    Side.SENDER   -> send-only
    Side.RECEIVER -> receive-only
    anything else (CLIENT/SERVER/PUBLISHER/SUBSCRIBER) -> duplex

This is what lets a strictly send-only Multicast connection be paired with
a strictly receive-only connection of some other protocol inside a single
CompositeUnit (see composite.py) -- each side's directionality is a direct,
explicit consequence of how it's configured, not a hidden mode flag.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct as pystruct

from .base import FramedConnection
from .config import ConnectionConfig, Side
from .framing import unpack_message, IRSDataError

logger = logging.getLogger("connmgr.multicast")


class _MulticastProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: MulticastConnection, unit: str):
        self._owner = owner
        self._unit = unit

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            header, payload = unpack_message(data)
        except IRSDataError:
            logger.warning("dropping malformed multicast datagram from %s (unit=%s)", addr, self._unit)
            return
        self._owner._dispatch_incoming(self._unit, header.opcode, payload)

    def error_received(self, exc: Exception) -> None:
        logger.warning("multicast error on unit %s: %s", self._unit, exc)


class MulticastConnection(FramedConnection):
    """
    config.extra recognizes only:
      "ttl": int, send-side TTL (default 1 -- stays on the local subnet)

    Direction comes entirely from config.side -- see module docstring.
    """

    def __init__(self, config: ConnectionConfig):
        super().__init__(config)
        if config.side == Side.SENDER:
            self.can_send, self.can_receive = True, False
        elif config.side == Side.RECEIVER:
            self.can_send, self.can_receive = False, True
        else:
            self.can_send, self.can_receive = True, True
        self._transports: dict[str, asyncio.DatagramTransport] = {}

    async def _do_start(self) -> None:
        loop = asyncio.get_running_loop()
        for unit, endpoint in self.config.connections.items():
            port = endpoint.port
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

            if self.can_receive:
                sock.bind((self.config.local_ip, port))
                mreq = pystruct.pack(
                    "4s4s",
                    socket.inet_aton(self.config.ip),
                    socket.inet_aton(self.config.local_ip),
                )
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                logger.info("multicast unit %s joined group %s on %s", unit, self.config.ip, port)
            else:
                sock.bind((self.config.local_ip, 0))
                ttl = self.config.extra.get("ttl", 1)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
                logger.info("multicast unit %s ready to send to group %s:%s", unit, self.config.ip, port)

            transport, _protocol = await loop.create_datagram_endpoint(
                lambda unit=unit: _MulticastProtocol(self, unit), sock=sock
            )
            self._transports[unit] = transport
            # Multicast has no handshake: joining the group (or having a group
            # to send to) is as connected as a unit ever gets here.
            self._mark_unit_connected(unit)

    async def _do_send(self, unit_name: str, data: bytes, opcode: int) -> None:
        if not self.can_send:
            raise RuntimeError(f"multicast connection for unit {unit_name!r} is receive_only")
        transport = self._transports.get(unit_name)
        if transport is None:
            raise ConnectionError(f"multicast connection for unit {unit_name!r} not started")
        port = self.config.port_for_unit(unit_name)
        if port is None:
            raise ValueError(f"no configured port for unit {unit_name!r}")
        frame = self._frame(unit_name, data, opcode)
        transport.sendto(frame, (self.config.ip, port))

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """Close this unit's socket -- which also drops its multicast group
        membership -- after an echo timeout, leaving other units joined."""
        transport = self._transports.pop(unit_name, None)
        if transport is None:
            return
        logger.warning("multicast unit %s: leaving group after echo timeout", unit_name)
        transport.close()
        self._mark_unit_disconnected(unit_name)

    async def _do_stop(self) -> None:
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()
