"""
TCP connection implementation.

- side=Side.SERVER: one listening socket per configured unit's port. The latest
  client accepted on a port is that unit's peer; an earlier one is closed.
- side=Side.CLIENT: one outgoing connection per configured unit. If `local_ip`
  is configured, the socket is bound to it (ephemeral port). With
  `start(retry=True)` a client reconnects after its peer drops.

One TcpConnection manages every configured port behind one send/receive surface,
routed by unit name.
"""
from __future__ import annotations

import asyncio
import logging

from .base import FramedConnection
from .config import ConnectionConfig, Side
from .framing import HEADER_SIZE, unpack_header

logger = logging.getLogger("connmgr.tcp")

#: Seconds between reconnect attempts of a client whose peer dropped.
RECONNECT_DELAY = 1.0


class TcpConnection(FramedConnection):
    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._servers: list[asyncio.base_events.Server] = []
        self._writers: dict[str, asyncio.StreamWriter] = {}  # unit_name -> active peer writer
        # Per unit, so a slow peer's drain() never holds up the others.
        self._write_locks: dict[str, asyncio.Lock] = {}

    async def _do_start(self) -> None:
        if self.config.side == Side.SERVER:
            for unit_name, endpoint in self.config.connections.items():
                server = await asyncio.start_server(
                    lambda r, w, unit=unit_name: self._on_client(unit, r, w),
                    host=self.config.local_ip,
                    port=endpoint.port,
                )
                self._servers.append(server)
                logger.info("TCP server listening on %s:%s (unit=%s)",
                            self.config.local_ip, endpoint.port, unit_name)
        else:
            for unit_name in self.config.connections:
                await self._connect(unit_name)

    async def _connect(self, unit_name: str) -> None:
        port = self.config.connections[unit_name].port
        # TODO allow setting the local port via config; 0 lets the OS pick one.
        local_addr = (self.config.local_ip, 0) if self.config.local_ip else None
        reader, writer = await asyncio.open_connection(self.config.ip, port, local_addr=local_addr)
        self._writers[unit_name] = writer
        self._track(self._read_loop(unit_name, reader, writer))
        logger.info("TCP client connected to %s:%s (unit=%s)", self.config.ip, port, unit_name)
        self._mark_unit_connected(unit_name)

    async def _reconnect(self, unit_name: str) -> None:
        while not self._closing:
            try:
                await self._connect(unit_name)
                return
            except OSError as exc:
                logger.debug("TCP unit %s: reconnect failed (%s)", unit_name, exc)
                await asyncio.sleep(RECONNECT_DELAY)

    def _on_client(self, unit_name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        logger.info("TCP unit %s: client connected from %s", unit_name, writer.get_extra_info("peername"))
        previous = self._writers.get(unit_name)
        self._writers[unit_name] = writer
        if previous is not None:
            # One peer per unit: the newcomer wins, the old socket is closed.
            previous.close()
            self._mark_unit_disconnected(unit_name)
        self._track(self._read_loop(unit_name, reader, writer))
        self._mark_unit_connected(unit_name)

    async def _read_loop(
        self, unit_name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Read framed messages for one unit until the peer goes away."""
        try:
            while True:
                header = unpack_header(await reader.readexactly(HEADER_SIZE))
                payload = await reader.readexactly(header.data_length)
                self._dispatch_incoming(unit_name, header.opcode, payload)
        except asyncio.IncompleteReadError:
            logger.info("TCP peer for unit %s closed the connection", unit_name)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            # An RST is a peer going away, not a bug: no traceback.
            logger.info("TCP peer for unit %s went away: %s", unit_name, exc)
        except Exception:
            logger.exception("TCP read loop for unit %s failed", unit_name)
        finally:
            self._on_peer_lost(unit_name, writer)

    def _on_peer_lost(self, unit_name: str, writer: asyncio.StreamWriter) -> None:
        """Close the dead socket and retire the unit -- unless a newer peer has
        already replaced this one. A client with `retry` reconnects."""
        writer.close()
        if self._writers.get(unit_name) is writer:
            del self._writers[unit_name]
            self._mark_unit_disconnected(unit_name)
        elif unit_name in self._writers:
            return
        if self.config.side == Side.CLIENT and self._retry and not self._closing:
            self._track(self._reconnect(unit_name))

    async def _do_send(self, unit_name: str, data: bytes, opcode: int) -> None:
        writer = self._writers.get(unit_name)
        if writer is None:
            raise ConnectionError(f"No active TCP peer for unit {unit_name!r}")
        frame = self._frame(data, opcode)
        lock = self._write_locks.setdefault(unit_name, asyncio.Lock())
        async with lock:
            writer.write(frame)
            await writer.drain()

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """Close only this unit's peer socket. A server keeps listening, so the
        peer can come back; a client with `retry` reconnects."""
        writer = self._writers.pop(unit_name, None)
        if writer is None:
            return
        logger.warning("TCP unit %s: closing peer socket", unit_name)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _do_stop(self) -> None:
        for server in self._servers:
            server.close()

        for writer in list(self._writers.values()):
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._writers.clear()

        # Python <= 3.12: wait_closed() also waits for every accepted
        # connection, so the peer writers above must be closed first.
        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()
