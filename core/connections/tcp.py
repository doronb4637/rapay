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
from .config import ConnectionConfig, Side
from .framing import HEADER_SIZE, unpack_header

logger = logging.getLogger("connmgr.tcp")


class TcpConnection(FramedConnection):
    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._servers: list[asyncio.base_events.Server] = []
        self._writers: dict[str, asyncio.StreamWriter] = {}  # unit_name -> active peer writer
        self._write_lock: asyncio.Lock | None = None

    async def _do_start(self) -> None:
        self._write_lock = asyncio.Lock()
        if self.config.side == Side.SERVER:
            for unit_name, endpoint in self.config.connections.items():
                port = endpoint.port
                server = await asyncio.start_server(
                    lambda r, w, unit=unit_name: asyncio.ensure_future(self._on_client(unit, r, w)),
                    host=self.config.local_ip,
                    port=port,
                )
                self._servers.append(server)
                logger.info("TCP server listening on %s:%s (unit=%s)", self.config.local_ip, port, unit_name)
        else:
            for unit_name, endpoint in self.config.connections.items():
                port = endpoint.port
                # port 0 = Lets the OS pick a random free port.
                # port = self.config.local_port if self.config.local_port else 0 TODO allow to set local_port via config
                local_addr = (self.config.local_ip, 0) if self.config.local_ip else None
                reader, writer = await asyncio.open_connection(
                    self.config.ip, port, local_addr=local_addr
                )
                self._writers[unit_name] = writer
                self._track(self._read_loop(unit_name, reader, writer))
                logger.info("TCP client connected to %s:%s (unit=%s)", self.config.ip, port, unit_name)
                self._mark_unit_connected(unit_name)

    async def _on_client(self, unit_name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("TCP unit %s: client connected from %s", unit_name, peer)
        previous = self._writers.get(unit_name)
        self._writers[unit_name] = writer
        if previous is not None and previous is not writer:
            self._mark_unit_disconnected(unit_name)
        self._track(self._read_loop(unit_name, reader, writer))
        self._mark_unit_connected(unit_name)

    async def _read_loop(
        self, unit_name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Continuously read, frame, and dispatch incoming TCP messages for a unit.

        Extracts the opcode and payload length from the message header, then
        consumes the exact payload bytes to maintain stream synchronization.
        """
        try:
            while True:
                header_bytes = await reader.readexactly(HEADER_SIZE)
                header = unpack_header(header_bytes)
                payload = await reader.readexactly(header.data_length)
                self._dispatch_incoming(unit_name, header.opcode, payload)
        except asyncio.IncompleteReadError:
            logger.info("TCP peer for unit %s closed the connection", unit_name)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.info("TCP peer for unit %s went away: %s", unit_name, exc)
        except Exception:
            logger.exception("TCP read loop for unit %s failed", unit_name)
        finally:
            self._on_peer_lost(unit_name, writer)

    def _on_peer_lost(self, unit: str, writer: asyncio.StreamWriter) -> None:
        """Remove a disconnected peer and mark the unit as disconnected.

        If the unit has already reconnected with a newer writer instance, the
        active registration and connection status remain untouched.
        """
        # TODO
        """ Always close the dying socket regardless of whether it was replaced """
        # try:
        #     writer.close()
        # except Exception:
        #     pass
        # TODO UNTIL HERE
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
        # Only lets one owner write to the stream at a time.
        async with self._write_lock:
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
        for server in self._servers:
            server.close()

        for unit, writer in list(self._writers.items()):
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._writers.clear()

        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()
