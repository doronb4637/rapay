"""
TCP connection implementation.

- side=Side.SERVER: one asyncio.start_server per configured port. Each port
  is mapped to a unit via config.units. New client sockets on that port
  become the active peer for sends to that unit.
- side=Side.CLIENT: one asyncio.open_connection per configured port/unit.
  If `local_ip` is configured, the outgoing socket is explicitly bound to
  it (with an ephemeral port, i.e. port 0).

A single TcpConnection instance transparently manages *all* configured ports
-- the caller only ever sees one object with one send_message/
receive_message surface, routed internally by unit_name.
"""
from __future__ import annotations

import asyncio
import logging

from .base import FramedConnection
from .config import Side
from .framing import HEADER_SIZE, unpack_header

logger = logging.getLogger("connmgr.tcp")


class TcpConnection(FramedConnection):
    def __init__(self, config):
        super().__init__(config)
        self._servers: list[asyncio.base_events.Server] = []
        self._writers: dict[str, asyncio.StreamWriter] = {}  # unit_name -> active peer writer
        self._write_lock: asyncio.Lock | None = None

    def _unit_from_port(self, port: int) -> str:
        unit = self.config.unit_from_port(port)
        if unit is None:
            raise ValueError(f"no unit configured for port {port}; check config['units']")
        return unit

    async def _do_start(self) -> None:
        self._write_lock = asyncio.Lock()
        if self.config.side == Side.SERVER:
            for port in self.config.ports:
                unit = self._unit_from_port(port)
                server = await asyncio.start_server(
                    lambda r, w, unit=unit: asyncio.ensure_future(self._on_client(unit, r, w)),
                    host=self.config.local_ip,
                    port=port,
                )
                self._servers.append(server)
                logger.info("TCP server listening on %s:%s (unit=%s)", self.config.local_ip, port, unit)
        else:
            for port in self.config.ports:
                unit = self._unit_from_port(port)
                # If local_ip is configured, explicitly bind the outgoing
                # client socket to it (port 0 = let the OS pick an ephemeral
                # source port) rather than letting the OS pick the source
                # interface too.
                local_addr = (self.config.local_ip, 0) if self.config.local_ip else None
                reader, writer = await asyncio.open_connection(
                    self.config.ip, port, local_addr=local_addr
                )
                self._writers[unit] = writer
                self._track(self._read_loop(unit, reader))
                logger.info("TCP client connected to %s:%s (unit=%s)", self.config.ip, port, unit)

    async def _on_client(self, unit: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # The newest inbound client on a unit's port becomes the active send
        # target for that unit. (A fan-out server that must address many
        # simultaneous clients per unit would extend this to a list/dict
        # keyed by peer address -- left simple here per the stated scope.)
        peer = writer.get_extra_info("peername")
        logger.info("TCP unit %s: client connected from %s", unit, peer)
        self._writers[unit] = writer
        self._track(self._read_loop(unit, reader))

    async def _read_loop(self, unit: str, reader: asyncio.StreamReader) -> None:
        """Parses the (UnitCode,OpCode,DataLength) header out of the TCP
        byte-stream, then reads exactly that many payload bytes -- correctly
        handling the fact that TCP gives no message boundaries of its own."""
        try:
            while True:
                header_bytes = await reader.readexactly(HEADER_SIZE)
                header = unpack_header(header_bytes)
                payload = await reader.readexactly(header.data_length)
                self._dispatch_incoming(unit, header.opcode, payload)
        except asyncio.IncompleteReadError:
            logger.info("TCP peer for unit %s closed the connection", unit)
        except asyncio.CancelledError:
            raise  # let stop()'s gather() see the cancellation cleanly
        except Exception:
            logger.exception("TCP read loop for unit %s failed", unit)

    async def _do_send(self, unit_name: str, data: bytes, opcode: int) -> None:
        writer = self._writers.get(unit_name)
        if writer is None:
            raise ConnectionError(f"No active TCP peer for unit {unit_name!r}")
        frame = self._frame(unit_name, data, opcode)
        assert self._write_lock is not None
        async with self._write_lock:  # interleaved sends from concurrent callers stay atomic
            writer.write(frame)
            await writer.drain()

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """Close only this unit's peer socket (echo-timeout watchdog). The
        listening server for that port stays up, so a peer that comes back
        can reconnect and re-arm the unit through _on_client()."""
        writer = self._writers.pop(unit_name, None)
        if writer is None:
            return
        logger.warning("TCP unit %s: closing peer socket after echo timeout", unit_name)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _do_stop(self) -> None:
        # 1. Stop ACCEPTING new connections immediately. server.close() alone
        #    already achieves this -- no new client can complete a handshake
        #    on this listening socket from this point on.
        for server in self._servers:
            server.close()

        # 2. Close every open peer socket next, *before* awaiting
        #    wait_closed() below. This ordering matters: on Python <=3.12,
        #    asyncio.Server.wait_closed() blocks until BOTH the listening
        #    socket is closed AND every connection it ever accepted has
        #    finished -- so awaiting it while a peer writer is still open
        #    deadlocks forever. Closing peers first guarantees wait_closed()
        #    can actually complete.
        for unit, writer in list(self._writers.items()):
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._writers.clear()

        # 3. Now it's safe to wait for the listening sockets to fully close.
        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()
