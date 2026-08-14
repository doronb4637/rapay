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

    async def _do_start(self) -> None:
        self._write_lock = asyncio.Lock()
        if self.config.side == Side.SERVER:
            for unit, endpoint in self.config.connections.items():
                port = endpoint.port
                server = await asyncio.start_server(
                    lambda r, w, unit=unit: asyncio.ensure_future(self._on_client(unit, r, w)),
                    host=self.config.local_ip,
                    port=port,
                )
                self._servers.append(server)
                logger.info("TCP server listening on %s:%s (unit=%s)", self.config.local_ip, port, unit)
        else:
            for unit, endpoint in self.config.connections.items():
                port = endpoint.port
                # Bind the source interface explicitly when local_ip is set
                # (port 0 = OS picks the ephemeral source port).
                local_addr = (self.config.local_ip, 0) if self.config.local_ip else None
                reader, writer = await asyncio.open_connection(
                    self.config.ip, port, local_addr=local_addr
                )
                self._writers[unit] = writer
                self._track(self._read_loop(unit, reader, writer))
                logger.info("TCP client connected to %s:%s (unit=%s)", self.config.ip, port, unit)
                # The dial succeeded, so this unit has a peer right now.
                self._mark_unit_connected(unit)

    async def _on_client(self, unit: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # The newest inbound client on a unit's port becomes that unit's send
        # target. A fan-out server would key a dict by peer address instead.
        peer = writer.get_extra_info("peername")
        logger.info("TCP unit %s: client connected from %s", unit, peer)
        previous = self._writers.get(unit)
        self._writers[unit] = writer
        if previous is not None and previous is not writer:
            # Superseded peer: drop the unit first so its echo is rebuilt
            # around the new socket, not left aimed at the old one.
            self._mark_unit_disconnected(unit)
        self._track(self._read_loop(unit, reader, writer))
        # A server-side unit becomes reachable here -- and so does its echo,
        # including on a reconnect after an earlier peer dropped.
        self._mark_unit_connected(unit)

    async def _read_loop(
        self, unit: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Parses the (UnitCode,OpCode,DataLength) header out of the TCP
        byte-stream, then reads exactly that many payload bytes -- correctly
        handling the fact that TCP gives no message boundaries of its own.

        Owns the unit's "still connected" claim for the lifetime of `writer`:
        when this loop ends, that peer is gone."""
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
        except OSError as exc:
            # A peer that vanishes instead of closing politely (RST, killed
            # process, interface down) is routine, so report it like the
            # graceful close above -- not as a traceback.
            logger.info("TCP peer for unit %s went away: %s", unit, exc)
        except Exception:
            logger.exception("TCP read loop for unit %s failed", unit)
        finally:
            self._on_peer_lost(unit, writer)

    def _on_peer_lost(self, unit: str, writer: asyncio.StreamWriter) -> None:
        """Retire `writer` as `unit`'s peer -- unless a newer one already
        replaced it, in which case this loop is just the old socket finishing
        and the unit is still very much connected."""
        if self._writers.get(unit) is not writer:
            return
        del self._writers[unit]
        self._mark_unit_disconnected(unit)

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
        # 1. Stop accepting immediately; server.close() alone does that.
        for server in self._servers:
            server.close()

        # 2. Close peer sockets BEFORE awaiting wait_closed() below: on
        #    Python <=3.12 it waits for every accepted connection too, so
        #    awaiting it with a peer writer still open deadlocks forever.
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
