"""
Multicast connection: UDP datagrams plus IP_ADD_MEMBERSHIP group joins.

Direction comes entirely from `config.side`:

    Side.SENDER          -> send-only
    Side.RECEIVER        -> receive-only
    Side.CLIENT/SERVER   -> duplex
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import sys

from core.IRS.irs_parser import IRSDataError

from .base import FramedConnection
from .config import ConnectionConfig, Side
from .framing import unpack_message

logger = logging.getLogger("connmgr.multicast")

MCAST_GROUP_REQ = struct.Struct("4s4s")


class _MulticastProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: MulticastConnection, unit_name: str) -> None:
        self._owner = owner
        self._unit_name = unit_name

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            header, payload = unpack_message(data)
        except IRSDataError:
            logger.warning("dropping malformed multicast datagram from %s (unit=%s)", addr, self._unit_name)
            return
        if self._owner._is_own(header.unit_code):
            return
        # Re-arms a unit the echo watchdog marked down.
        self._owner._mark_unit_connected(self._unit_name)
        self._owner._dispatch_incoming(self._unit_name, header.opcode, payload)

    def error_received(self, exc: Exception) -> None:
        logger.warning("multicast error on unit %s: %s", self._unit_name, exc)


class MulticastConnection(FramedConnection):
    """
    `ip` is the multicast group. config.extra recognizes only `"ttl"`: the
    send-side hop limit, 0-255 (default 1 -- stays on the local subnet).
    """

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self.can_send = config.side != Side.RECEIVER
        self.can_receive = config.side != Side.SENDER
        ttl = config.extra.get("ttl", 1)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 0 <= ttl <= 255:
            raise ValueError(f"config['ttl'] must be an integer 0-255, got {ttl!r}")
        self._ttl = ttl
        self._transports: dict[str, asyncio.DatagramTransport] = {}

    def _is_own(self, sender_code: int) -> bool:
        """A duplex member hears its own sends through multicast loopback."""
        return self.can_send and self.can_receive and sender_code == self._own_unit_code

    def _open_socket(self, unit_name: str, port: int) -> socket.socket:
        group = self.config.ip
        local_ip = self.config.local_ip
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            if self.can_receive:
                # Windows receives group traffic on a socket bound to the
                # interface; elsewhere that filters it out, so bind to any.
                sock.bind((local_ip if sys.platform == "win32" else "", port))
                mreq = MCAST_GROUP_REQ.pack(socket.inet_aton(group), socket.inet_aton(local_ip))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                logger.info("multicast unit %s joined group %s:%s", unit_name, group, port)
            else:
                sock.bind((local_ip, 0))
            if self.can_send:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self._ttl)
                if local_ip != "0.0.0.0":
                    # Send through the configured NIC, not the default route's.
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
                logger.info("multicast unit %s ready to send to group %s:%s", unit_name, group, port)
        except BaseException:
            sock.close()
            raise
        return sock

    async def _do_start(self) -> None:
        loop = asyncio.get_running_loop()
        for unit_name, endpoint in self.config.connections.items():
            sock = self._open_socket(unit_name, endpoint.port)
            try:
                transport, _protocol = await loop.create_datagram_endpoint(
                    lambda unit=unit_name: _MulticastProtocol(self, unit), sock=sock
                )
            except BaseException:
                sock.close()
                raise
            self._transports[unit_name] = transport
            self._mark_unit_connected(unit_name)

    async def _do_send(self, unit_name: str, data: bytes, opcode: int) -> None:
        if not self.can_send:
            raise RuntimeError(f"multicast connection for unit {unit_name!r} is receive_only")
        transport = self._transports.get(unit_name)
        if transport is None:
            raise ConnectionError(f"multicast connection for unit {unit_name!r} not started")
        port = self.config.connections[unit_name].port
        transport.sendto(self._frame(data, opcode), (self.config.ip, port))

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """Keep the socket and the group membership: the next datagram from the
        unit marks it connected again."""
        logger.warning("multicast unit %s: marked down; waiting for it to send again", unit_name)

    async def _do_stop(self) -> None:
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()
