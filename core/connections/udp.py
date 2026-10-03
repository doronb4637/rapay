"""
UDP connection implementation, on asyncio's DatagramProtocol: inbound packets
arrive through one non-blocking `datagram_received` callback per unit.
"""
from __future__ import annotations

import asyncio
import logging

from core.IRS.irs_parser import IRSDataError

from .base import FramedConnection
from .config import ConnectionConfig, Side
from .framing import unpack_message


logger = logging.getLogger("connmgr.udp")

#: (ip, port) pair.
PeerAddress = tuple[str, int]


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: UdpConnection, unit_name: str) -> None:
        self._owner = owner
        self._unit = unit_name

    def datagram_received(self, data: bytes, addr: PeerAddress) -> None:
        try:
            header, payload = unpack_message(data)
        except IRSDataError:
            logger.warning("dropping malformed UDP datagram from %s (unit=%s)", addr, self._unit)
            return
        self._owner._remember_peer(self._unit, addr)
        self._owner._dispatch_incoming(self._unit, header.opcode, payload)

    def error_received(self, exc: Exception) -> None:
        logger.warning("UDP error on unit %s: %s", self._unit, exc)


class UdpConnection(FramedConnection):
    """
    config.extra["mode"] optionally restricts direction: "send_only" |
    "receive_only" | "duplex" (default), so a plain UDP link can be one
    direction-limited half of a `CompositeUnit`.

    A server sends each unit's traffic to whichever address last sent on that
    unit's port.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        mode = config.extra.get("mode", "duplex")
        if mode not in ("send_only", "receive_only", "duplex"):
            raise ValueError(f"invalid udp mode {mode!r}")
        self.can_send = mode in ("send_only", "duplex")
        self.can_receive = mode in ("receive_only", "duplex")
        self._transports: dict[str, asyncio.DatagramTransport] = {}
        self._peers: dict[str, PeerAddress] = {}  # unit -> address to send to

    async def _do_start(self) -> None:
        loop = asyncio.get_running_loop()
        for unit_name, endpoint in self.config.connections.items():
            port = endpoint.port
            if self.config.side == Side.SERVER:
                local_addr = (self.config.local_ip, port)
                remote_addr = None
            else:
                local_addr = (self.config.local_ip, 0)  # ephemeral local port
                remote_addr = (self.config.ip, port)
                self._peers[unit_name] = remote_addr

            transport, _protocol = await loop.create_datagram_endpoint(
                lambda unit=unit_name: _DatagramProtocol(self, unit),
                local_addr=local_addr,
                remote_addr=remote_addr,
            )
            self._transports[unit_name] = transport
            logger.info("UDP %s bound for unit %s on %s", self.config.side.value, unit_name, local_addr)
            if remote_addr is not None:
                self._mark_unit_connected(unit_name)  # the destination is known: we can send

    def _remember_peer(self, unit: str, addr: PeerAddress) -> None:
        self._peers[unit] = addr
        self._mark_unit_connected(unit)

    async def _do_send(self, unit_name: str, data: bytes, opcode: int) -> None:
        if not self.can_send:
            raise RuntimeError(f"UDP connection for unit {unit_name!r} is receive_only")
        transport = self._transports.get(unit_name)
        if transport is None:
            raise ConnectionError(f"UDP connection for unit {unit_name!r} not started")
        peer = self._peers.get(unit_name)
        if peer is None:
            raise ConnectionError(
                f"UDP unit {unit_name!r}: no peer address known yet (nothing received from it)")
        transport.sendto(self._frame(data, opcode), peer)

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """Forget a server's learned peer but keep the socket open, so the next
        datagram from the unit reconnects it."""
        if self.config.side == Side.SERVER:
            self._peers.pop(unit_name, None)
        logger.warning("UDP unit %s: marked down; waiting for it to send again", unit_name)

    async def _do_stop(self) -> None:
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()
        self._peers.clear()
