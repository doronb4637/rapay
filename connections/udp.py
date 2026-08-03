"""
UDP connection implementation, built on asyncio's DatagramProtocol so a
single event-loop callback (`datagram_received`) handles inbound packets
with zero blocking reads.
"""
from __future__ import annotations

import asyncio
import logging

from .base import FramedConnection
from .config import Side
from .framing import unpack_message, IRSDataError

logger = logging.getLogger("connmgr.udp")


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: UdpConnection, unit: str):
        self._owner = owner
        self._unit = unit

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        pass

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            header, payload = unpack_message(data)
        except IRSDataError:
            logger.warning("dropping malformed UDP datagram from %s (unit=%s)", addr, self._unit)
            return
        self._owner._remember_peer(self._unit, addr)
        # Safe to call directly here: this callback always runs on the
        # connection's owning event loop thread, same as _dispatch_incoming.
        self._owner._dispatch_incoming(self._unit, header.opcode, payload)

    def error_received(self, exc: Exception) -> None:
        logger.warning("UDP error on unit %s: %s", self._unit, exc)


class UdpConnection(FramedConnection):
    """
    config.extra["mode"] optionally restricts direction: "send_only" |
    "receive_only" | "duplex" (default). This lets a plain UDP link stand in
    as one direction-limited half of a composite Unit (see composite.py).
    """

    def __init__(self, config):
        super().__init__(config)
        mode = config.extra.get("mode", "duplex")
        if mode not in ("send_only", "receive_only", "duplex"):
            raise ValueError(f"invalid udp mode {mode!r}")
        self.can_send = mode in ("send_only", "duplex")
        self.can_receive = mode in ("receive_only", "duplex")
        self._transports: dict[str, asyncio.DatagramTransport] = {}
        self._peers: dict[str, tuple[str, int]] = {}  # unit -> address to send to

    def _remember_peer(self, unit: str, addr) -> None:
        self._peers[unit] = addr

    def _unit_from_port(self, port: int) -> str:
        unit = self.config.unit_from_port(port)
        if unit is None:
            raise ValueError(f"no unit configured for port {port}; check config['units']")
        return unit

    async def _do_start(self) -> None:
        loop = asyncio.get_running_loop()
        for port in self.config.ports:
            unit = self._unit_from_port(port)
            if self.config.side == Side.SERVER:
                local_addr = (self.config.local_ip, port)
                remote_addr = None
            else:
                local_addr = (self.config.local_ip, 0)  # ephemeral local port
                remote_addr = (self.config.ip, port)
                self._peers[unit] = remote_addr  # known target even before any inbound reply

            transport, _protocol = await loop.create_datagram_endpoint(
                lambda unit=unit: _DatagramProtocol(self, unit),
                local_addr=local_addr,
                remote_addr=remote_addr,
            )
            self._transports[unit] = transport
            logger.info("UDP %s bound for unit %s on %s", self.config.side.value, unit, local_addr)

    async def _do_send(self, unit_name: str, data: bytes, opcode: int) -> None:
        if not self.can_send:
            raise RuntimeError(f"UDP connection for unit {unit_name!r} is receive_only")
        transport = self._transports.get(unit_name)
        if transport is None:
            raise ConnectionError(f"UDP connection for unit {unit_name!r} not started")
        frame = self._frame(unit_name, data, opcode)
        peer = self._peers.get(unit_name)
        if peer is not None:
            transport.sendto(frame, peer)
        else:
            transport.sendto(frame)  # transport already has remote_addr bound

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """Close only this unit's datagram endpoint (echo-timeout watchdog);
        every other unit on this connection keeps its own socket."""
        transport = self._transports.pop(unit_name, None)
        if transport is None:
            return
        logger.warning("UDP unit %s: closing endpoint after echo timeout", unit_name)
        transport.close()
        self._peers.pop(unit_name, None)

    async def _do_stop(self) -> None:
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()
