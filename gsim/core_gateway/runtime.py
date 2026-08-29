"""
GSim's own connection registry, message logs, and the thread bridge that gets
`core`'s callbacks out to the UI.

Three things about `core` drive this design:

1. **`Connection`'s public API is blocking and synchronous** -- `send_message`,
   `start`, `close` each marshal onto core's single background event loop and
   block the caller (`base._EventLoopThread.await_coroutine`). They must never
   be awaited from the FastAPI event loop. Every route that touches this module
   is therefore declared `def`, not `async def`, so Starlette runs it in its
   threadpool.

2. **Inbound callbacks arrive on executor threads.** `Connection._run_callback`
   invokes handlers via `run_in_executor(...)`, so a callback body runs on an
   arbitrary worker thread with no event loop. Publishing to WebSocket clients
   therefore hops threads through `loop.call_soon_threadsafe`.

3. **GSim owns the registry, `ConnectionManager` is used as a factory.** Core's
   manager has `create`/`get`/`shutdown_all` but no per-connection removal, and
   `_connections` is private. Rather than reach into it, GSim keeps its own
   record dict as the UI's source of truth and lets the manager keep its entry
   as an exit-time safety net -- `Connection.close()` is documented idempotent,
   so `shutdown_all()` double-closing an already-deleted connection is a no-op.
"""
from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from . import bootstrap  # noqa: F401

from core.connections.manager import ConnectionManager

from . import registry as message_registry
from .behaviours import BehaviourEngine
from .payloads import prepare_message
from .timing import wall_time

#: Per-connection ring buffers. Bounded so a chatty link cannot grow without
#: limit; the UI streams live events and only re-reads these on (re)connect.
LOG_LIMIT = 2000

#: EVERY message is published. A 1kHz route is genuinely ~1000 events a second
#: per direction, and the console has to be able to show that a 1ms schedule is
#: really firing at 1ms -- a sampled feed makes consecutive rows read as the
#: sampling period, which is indistinguishable from the scheduler being slow.
#: What keeps that affordable is BATCHING, not dropping: `api/routes/events.py`
#: coalesces whatever piled up during one WebSocket write into a single frame,
#: so the frame rate falls out of how fast the client drains rather than a
#: constant, and no entry is ever lost on the way.


@dataclass
class LogEntry:
    seq: int
    direction: str          # "sent" | "received"
    connection_name: str    # which GSim connection owns this entry (the console is global)
    unit_name: str          # OUR name when sent, the SENDER's configured name when received
    unit_code: int          # the code the layout is registered under -- ours on send, theirs on receive
    op_code: int
    message_name: str
    payload: dict[str, Any] | None
    error: str | None
    timestamp: float
    #: Which structures module this entry's layout came from. Carried so the
    #: Inspector can re-fetch the schema for the SAME layout it was decoded
    #: with -- (unit_code, op_code) alone is ambiguous once two modules define
    #: it, which is the whole reason namespaces exist.
    namespace: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "direction": self.direction,
            "connection_name": self.connection_name,
            "unit_name": self.unit_name,
            "unit_code": self.unit_code,
            "namespace": self.namespace,
            "op_code": self.op_code,
            "op_code_hex": f"0x{self.op_code:04X}",
            "message_name": self.message_name,
            "payload": self.payload,
            "error": self.error,
            "timestamp": self.timestamp,
            # What the log line renders as: "[name]: [message]"
            "label": f"{self.unit_name}: {self.message_name}",
        }


class EventBus:
    """Fan-out from any thread to any number of asyncio consumers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[Any, Any]] = []   # (asyncio.loop, asyncio.Queue)

    def subscribe(self, loop, queue) -> None:
        with self._lock:
            self._subscribers.append((loop, queue))

    def unsubscribe(self, queue) -> None:
        with self._lock:
            self._subscribers = [(l, q) for l, q in self._subscribers if q is not queue]

    def publish(self, event: dict[str, Any]) -> None:
        """Safe to call from core's executor threads, its loop thread, or the
        API threadpool. Never raises into the caller: a dead subscriber must
        not take down a read loop."""
        with self._lock:
            targets = list(self._subscribers)
        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:
                # Loop already closed (client went away mid-publish).
                self.unsubscribe(queue)

    @staticmethod
    def _offer(queue, event: dict[str, Any]) -> None:
        """Enqueue one event, dropping it if the subscriber is behind.

        Runs ON the subscriber's loop, not in the publisher -- `put_nowait` was
        passed straight to `call_soon_threadsafe` before, so its `QueueFull`
        surfaced as an 'Exception in callback' traceback on uvicorn's loop for
        every event after the queue filled, rather than anywhere the publisher
        could see. A full queue only means the browser is not draining as fast
        as we publish, which a fast behaviour can genuinely cause; dropping is
        the correct response and has to be quiet to be useful. Nothing is lost
        permanently -- the ring buffers are the record, and a client re-syncs
        from the snapshot + `GET /api/logs/{direction}` backfill.
        """
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


@dataclass
class ConnectionRecord:
    #: The connection's name IS its identity -- there is no separate opaque id.
    #: Every route addresses it (`/api/connections/{name}/...`), `_conn_records`
    #: is keyed by it, and log entries and behaviours reference it. That is what
    #: makes a URL readable by hand; the cost is that names must be unique
    #: (enforced in `create`) and URL-safe (enforced in `api/models.py`), and
    #: that renaming is a change of identity -- see `replace`.
    name: str
    config: dict[str, Any]
    unit: Any                       # core Connection | CompositeUnit
    running: bool = False
    sent: deque = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    received: deque = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    #: Why this connection is not running, when an automatic start failed.
    #: A TCP client created before its server exists is the ordinary case --
    #: the config is perfectly valid, the peer simply is not listening yet --
    #: so `create()` keeps the connection and records the reason here instead
    #: of failing the request and throwing the config away.
    start_error: str | None = None
    #: Whether this connection's `handle_on_receive` callbacks are currently
    #: registered with core. Tracked separately from `running` because the two
    #: genuinely diverge: `start()` installs the handlers BEFORE `unit.start()`
    #: (a peer can have data waiting the instant the transport opens), so a
    #: start that fails to connect leaves handlers installed while `running`
    #: stays False. Using `running` as the guard meant the next start attempt
    #: registered every route a second time, and core rightly refuses that --
    #: "route ... already has an on-receive callback" -- which surfaced as a
    #: 500 on the one workflow this is most likely to happen in: bring up a TCP
    #: client before its server, then start it once the server is up.
    handlers_installed: bool = False

    @property
    def own_unit_code(self) -> int:
        return self.config["unitCode"]

    def peers(self) -> dict[str, int]:
        """Configured peer name -> that peer's unit code. This mapping is what
        lets a received message be labelled with the SENDER's defined name."""
        connections = self.config.get("connections", {})
        return {name: connected_unit["unitCode"] for name, connected_unit in connections.items()}

    def structures_for(self, unit_name: str) -> list[str]:
        """The IRS structures namespaces scoping ONE link's layouts.

        Read from core's PARSED config rather than re-derived from the raw dict:
        a structures spelling ("Test.messages", or a file path) is normalised to
        a module name by `tools.general.resolve_module_name`, and GSim resolving
        it a second time is exactly how the two would eventually disagree about
        which module a link uses.

        Empty means unscoped -- every registered module is searched, which is
        what a connection with no `Structures` gets.
        """
        config = getattr(self.unit, "config", None)
        if config is None:            # CompositeUnit has no config of its own
            return []
        try:
            return list(config.structures_for(unit_name))
        except ValueError:            # unit_name not configured on this connection
            return []

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": self.running,
            "protocol": self.config.get("protocol"),
            "side": self.config.get("side"),
            "unit_code": self.own_unit_code,
            "peers": [
                {"name": name, "unit_code": code} for name, code in self.peers().items()
            ],
            "active_units": sorted(getattr(self.unit, "active_units", set()) or set()),
            "config": self.config,
            "start_error": self.start_error,
        }


@dataclass(frozen=True)
class PreparedSend:
    """One resolved route plus its built IRS message, ready to fire N times.

    Built by `GSimRuntime.sender()`. Holding the built message across ticks is
    safe because nothing mutates it -- `Connection._encode` only calls
    `to_bytes()` on it (~6us), and the log payload is its `to_dict()` captured
    at build time. When a per-tick mutator is eventually added (a counter, a
    timestamp -- the extensibility `behaviours.py` was built around), mutating
    THIS message in place before `fire()` is precisely the seam it wants; the
    log payload would then be re-derived per tick and nothing else changes.

    It holds the `ConnectionRecord`, not the connection's name, so a tick does
    no dictionary lookup and cannot be re-pointed at a connection that was
    deleted and recreated under the same name. Its validity window is the
    caller's to manage -- for a behaviour, that is exactly its worker's
    lifetime, since `_sync` tears the worker down on stop, edit and delete.
    """
    runtime: GSimRuntime
    record: ConnectionRecord
    unit_name: str
    op_code: int
    message: Any
    message_name: str
    namespace: str | None
    payload: dict[str, Any]

    def fire(self) -> LogEntry:
        """Send it and log it."""
        self.record.unit.send_message(self.message, self.op_code, self.unit_name)
        return self.runtime._log(
            self.record, "sent", self.record.name, self.record.own_unit_code,
            self.op_code, self.message_name, self.payload, None,
            namespace=self.namespace,
        )


class GSimRuntime:
    """Process-wide singleton holding every GSim connection."""

    #: How often to resample per-unit connectivity. See `_watch_unit_state`.
    STATE_POLL_SECONDS = 0.5

    def __init__(self) -> None:
        self._manager = ConnectionManager()
        self._conn_records: dict[str, ConnectionRecord] = {}
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self.events = EventBus()
        # Scheduled sending. Given `self.send` -- the same call the manual send
        # button makes -- so every tick lands in the console as an ordinary
        # entry; see behaviours.py's docstring for why core's own
        # `periodic_sending` is not used here.
        self.behaviours = BehaviourEngine(
            sender=self.sender,
            is_connection_running=self._is_connection_running,
            publish=self.events.publish,
        )
        self._watcher = threading.Thread(
            target=self._watch_unit_state, name="gsim-unit-state", daemon=True
        )
        self._watcher.start()

    def _is_connection_running(self, connection_name: str) -> bool:
        """Unknown connection reads as "not running" rather than raising -- the
        behaviour engine polls this during teardown races."""
        with self._lock:
            record = self._conn_records.get(connection_name)
        return bool(record and record.running)

    def _watch_unit_state(self) -> None:
        """Publish `connection.state` whenever a connection's set of live peers
        changes.

        Core signals this internally -- `_mark_unit_connected` /
        `_mark_unit_disconnected` fire `_notify_state_change()` -- but that is
        an `asyncio.Event` on core's own loop with no public subscription hook;
        the only public surface is the `active_units` property. Without this,
        GSim would only ever learn about connectivity at the moments it happens
        to call `refresh()` (create/start/stop/delete), so a peer that connects
        *after* start -- a TCP client dialling in, a UDP server's first inbound
        datagram, an echo-timeout drop -- would leave the sidebar showing stale
        "offline" dots until the user toggled the connection off and on.

        Polling a public property is deliberate: subscribing to core's private
        `_state_event` would mean reaching into its internals, which is the one
        thing this package must not do.
        """
        previous: dict[str, set[str]] = {}
        while True:
            time.sleep(self.STATE_POLL_SECONDS)
            with self._lock:
                connection_records = list(self._conn_records.values())
            existing_conn = set()
            for conn_record in connection_records:
                existing_conn.add(conn_record.name)
                try:
                    record_active_unit = set(conn_record.unit.active_units)
                except Exception:  # a torn-down unit must not kill the watcher
                    continue
                if previous.get(conn_record.name) != record_active_unit:
                    previous[conn_record.name] = record_active_unit
                    self._publish_state(conn_record)
            # Pop the id of a deleted connection config.
            for deleted_conn in set(previous) - existing_conn:
                previous.pop(deleted_conn, None)

    # -- queries ---------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [conn_record.as_dict() for conn_record in self._conn_records.values()]

    def get(self, connection_name: str) -> ConnectionRecord:
        with self._lock:
            record = self._conn_records.get(connection_name)
        if record is None:
            raise KeyError(f"no connection named {connection_name!r}")
        return record

    def logs(self, connection_name: str, direction: str) -> list[dict[str, Any]]:
        record = self.get(connection_name)
        source = record.sent if direction == "sent" else record.received
        return [entry.as_dict() for entry in source]

    def all_logs(self, direction: str) -> list[dict[str, Any]]:
        """Every connection's log for one direction, in global order.

        The console is deliberately process-wide: a send and the matching
        receive are logged against DIFFERENT connections (the sender and the
        recipient), so a per-connection view can only ever show half of any
        exchange. Ordered by `seq` -- a single counter shared by every record,
        which wall-clock timestamps could not be (entries originate on
        different threads).
        """
        with self._lock:
            conn_records = list(self._conn_records.values())
        entries = [
            entry
            for record in conn_records
            for entry in (record.sent if direction == "sent" else record.received)
        ]
        entries.sort(key=lambda entry: entry.seq)
        return [entry.as_dict() for entry in entries]

    def clear_logs(self, direction: str) -> int:
        """Empty every connection's ring buffer for ONE direction, and tell
        every client to do the same.

        Server-side rather than a client-only view filter, because these deques
        are what `GET /api/logs/{direction}` backfills from: clearing only the
        browser's copy would put every entry straight back on the next refresh
        or WebSocket reconnect. Publishing the event is what keeps a second
        open client from continuing to show what was just cleared.

        The `seq` counter is deliberately NOT reset -- it orders entries across
        threads and connections, and restarting it would make surviving entries
        in the *other* pane sort against new ones incorrectly.
        """
        with self._lock:
            conn_records = list(self._conn_records.values())
        cleared = 0
        for record in conn_records:
            buffer = record.sent if direction == "sent" else record.received
            cleared += len(buffer)
            buffer.clear()
        self.events.publish({"type": "logs.cleared", "direction": direction})
        return cleared

    # -- lifecycle -------------------------------------------------------
    def create(self, name: str, config: dict[str, Any], autostart: bool = True) -> ConnectionRecord:
        """Build a connection through core's factory and register it under `name`.

        `ConnectionManager.create()` validates the config, imports the
        `Structures` modules (each populating its own namespace) and instantiates
        the protocol class -- all of it raising `ValueError` at this point
        rather than at first I/O, which is what lets the modal report a bad
        config inline.

        The name must be free. It is the identifier every route, log entry and
        behaviour uses, so a duplicate would not merely be confusing -- the
        second connection would take over the first's URLs and silently inherit
        its behaviours. Rejected here rather than in the request model because
        only the runtime knows what already exists.
        """
        with self._lock:
            if name in self._conn_records:
                raise ValueError(
                    f"a connection named {name!r} already exists; names identify "
                    f"connections and must be unique")

        unit = self._manager.create(name, config)
        record = ConnectionRecord(name=name, config=config, unit=unit)
        with self._lock:
            self._conn_records[name] = record
        connection_name = name

        # NOT installed here -- see `start()`. Nothing can arrive before the
        # connection actually starts, so there's no need to register early,
        # and `start()` is what has to (re-)install them anyway.
        if autostart:
            try:
                self.start(connection_name)
            except OSError as exc:
                # The CONFIG is fine, the peer just is not reachable yet -- a
                # TCP client created before its server is listening is the
                # ordinary case (WinError 1225 / ECONNREFUSED). Failing the
                # whole request here would discard a config the user just
                # filled in and leave the modal open on an error they can do
                # nothing about; core's own `close()` is idempotent, so
                # keeping the record stopped costs nothing. The reason rides
                # back on the record so the UI can say why it is not running.
                record.start_error = str(exc)
                record.running = False
                self._publish_state(record)
        else:
            self._publish_state(record)
        return record

    def start(self, connection_name: str) -> ConnectionRecord:
        """Starts the connection, (re-)installing its receive handlers first.

        `Connection.close()` clears every `handle_on_receive` registration as
        part of its teardown (`core/connections/base.py` `_shutdown_all` --
        "every standing callback is dropped"). Before this fix, GSim only
        ever installed handlers once, in `create()`: stopping a connection
        from the Sidebar and starting it again reconnected the transport
        just fine but left it with no callbacks at all, so inbound messages
        were silently discarded by core's own "no route owner -> drop"
        dispatch rule -- reproduced end to end, the "Received" pane simply
        never got an entry for anything sent afterward, and only fully
        recreating the connection (Edit, or restarting GSim) fixed it,
        because only `create()` used to call this.

        Installed before `unit.start()`, matching the ordering `create()`
        always used: a reconnecting peer can have data waiting the instant
        the transport opens, so the handler has to exist first.

        Guarded by `record.handlers_installed` -- NOT by `record.running` --
        because `unit.start()` is idempotent (a no-op if already started) but
        `handle_on_receive` is not: registering a route while its first
        registration is still live raises. The two flags diverge whenever a
        start fails after the handlers are in place, which is the everyday
        "TCP client started before its server" case; see the field's own note.
        """
        record = self.get(connection_name)
        if not record.handlers_installed:
            self._install_receive_handlers(record)
            record.handlers_installed = True
        record.unit.start()
        record.running = True
        record.start_error = None       # a successful start clears the excuse
        # Resumes any behaviour configured on this connection -- see
        # `BehaviourEngine._sync`: "enabled AND connection running" is one
        # condition, so start/stop need no separate resume bookkeeping.
        self.behaviours.sync_connection(connection_name)
        self._publish_state(record)
        return record

    def stop(self, connection_name: str) -> ConnectionRecord:
        record = self.get(connection_name)
        # Behaviours are paused BEFORE the transport goes down, matching
        # `delete()` -- a worker firing into a connection being torn down logs
        # a spurious failure for every tick in the gap. Closing first made that
        # gap real: at the old 16ms floor it cost a stray error or two, but a
        # 1ms schedule books ~1800 of them, which then sit on the behaviour as
        # `last_error` after a perfectly ordinary stop. `_sync` reads
        # `is_connection_running`, so `running` has to be false before the call.
        record.running = False
        self.behaviours.sync_connection(connection_name)
        record.unit.close()
        # `close()` drops every standing callback (core's `_shutdown_all`), so
        # they are genuinely gone and the next start must put them back.
        record.handlers_installed = False
        self._publish_state(record)
        return record

    def delete(self, connection_name: str) -> None:
        """Close the connection and drop GSim's record. Core's manager keeps
        its own (now inert) entry -- harmless, because `close()` is idempotent,
        so its eventual `shutdown_all()` is a no-op on this one."""
        record = self.get(connection_name)
        # Before closing: a worker mid-tick would otherwise send into a
        # connection being torn down and log a spurious failure.
        self.behaviours.remove_connection(connection_name)
        try:
            record.unit.close()
        finally:
            with self._lock:
                self._conn_records.pop(connection_name, None)
            self.events.publish({"type": "connection.deleted", "connection_name": connection_name})

    def replace(self, connection_name: str, name: str, config: dict[str, Any]) -> ConnectionRecord:
        """'Edit' = delete + recreate. `ConnectionConfig` is `frozen=True` by
        design (a live `Connection` caches state derived from it), so there is
        no in-place mutation path and none should be invented.

        An edit MAY rename, and because the name is the identity that is a
        change of identity: the connection is addressed at a new URL afterwards,
        and (as with any edit) its logs and behaviours do not survive -- `delete`
        drops both. Callers holding the old name must follow the returned record.

        Renaming ONTO another connection's name is refused before anything is
        torn down, so a rejected edit cannot leave the original deleted.

        The POSITION is restored. `_conn_records` is an ordinary dict and the
        sidebar renders it in insertion order, so a plain delete-then-create
        re-inserts the rebuilt connection at the END -- editing the top entry
        visibly dropped it to the bottom of the list. Reinserting it where it
        was keeps "edit" from looking like "move".
        """
        with self._lock:
            order = list(self._conn_records)
            if name != connection_name and name in self._conn_records:
                raise ValueError(
                    f"a connection named {name!r} already exists; names identify "
                    f"connections and must be unique")
        position = order.index(connection_name) if connection_name in order else len(order)

        was_running = self.get(connection_name).running
        self.delete(connection_name)
        record = self.create(name, config, autostart=was_running)

        with self._lock:
            # `create` appended it; rebuild the mapping with it back in place.
            # Cheap -- there are a handful of connections, and doing it here
            # keeps every reader (list(), the snapshot, all_logs()) ordered
            # without any of them needing to know an edit happened.
            rebuilt = {key: value for key, value in self._conn_records.items() if key != name}
            items = list(rebuilt.items())
            items.insert(position, (name, record))
            self._conn_records.clear()
            self._conn_records.update(items)
        return record

    def shutdown(self) -> None:
        self.behaviours.shutdown()      # stop the workers before their targets vanish
        self._manager.shutdown_all()
        with self._lock:
            self._conn_records.clear()

    # -- messaging -------------------------------------------------------
    def sender(self, connection_name: str, unit_name: str, op_code: int,
               payload: dict[str, Any]) -> PreparedSend:
        """Resolve a route and build its message ONCE, for firing repeatedly.

        This exists because `prepare_message` costs ~160us for a 35-element
        `ArrayOfAreas` (route lookup + `from_dict` + `fill()` + `to_dict()`),
        which is a sixth of the entire budget for a 1 ms behaviour -- spent
        re-deriving a payload that, for `periodic`, cannot have changed. A
        behaviour worker builds one of these when it starts and fires it every
        tick; the manual send path builds one and fires it once.

        Deliberately the only new entry point: `send()` below is now written in
        terms of it, so a scheduled tick and the Send button still travel
        exactly ONE code path. That identity is the whole reason
        `behaviours.py` drives its own schedules instead of using core's
        `periodic_sending` -- it must not be quietly given up for speed.

        Raises the same things `send()` always did (`KeyError` for an unknown
        connection or unresolvable route, `IRSAmbiguousError`, `ValueError`),
        and raises them at BUILD time, so a schedule that could never produce a
        valid message fails when it is configured rather than once per tick.
        """
        record = self.get(connection_name)
        # Scoped by the DESTINATION: our own unit code is the same for every
        # peer, so it alone cannot say which link's layout this opcode means.
        structures = record.structures_for(unit_name)
        prepared = prepare_message(record.own_unit_code, op_code, structures, payload)
        return PreparedSend(
            runtime=self, record=record, unit_name=unit_name, op_code=op_code,
            message=prepared.message, message_name=prepared.name,
            namespace=prepared.namespace, payload=prepared.payload,
        )

    def send(self, connection_name: str, unit_name: str, op_code: int,
             payload: dict[str, Any]) -> dict[str, Any]:
        """Normalise, encode, send, and log it.

        The payload is built into a real IRS message here rather than handed to
        core as a dict. `_encode` passes any non-`bytes` straight to
        `irs_to_bytes`, which calls `message.to_bytes()` on a message and does its
        own route lookup + `from_dict` on a dict -- so building it once here is
        also what stops that work happening twice per send.

        What is logged is the message's own `to_dict()`, which is why a sent entry
        spells enums as member names exactly as a received one always has.
        """
        return self.sender(connection_name, unit_name, op_code, payload).fire().as_dict()

    # -- internals -------------------------------------------------------
    def _install_receive_handlers(self, record: ConnectionRecord) -> None:
        """Register one standing callback per (peer, opcode) that peer may send.

        The opcode set comes from the registry keyed by the PEER's unit code and
        scoped to THAT PEER's structures modules -- the same lookup `parse_irs`
        performs when decoding -- so every route registered here is one core will
        accept (`handle_on_receive` eagerly `validate_irs`-es and would raise
        otherwise). Scoping matters beyond correctness of the layout: unscoped,
        this registered every opcode any module had ever assigned to that unit
        code, on every peer that happened to share it.

        `unit_name` is closed over rather than passed by core: callbacks are
        invoked as `callback(message)` with no route context, and it is exactly
        the sender's *configured* name the Received log must display.
        """
        for unit_name, peer_code in record.peers().items():
            structures = record.structures_for(unit_name)
            for summary in message_registry.list_messages(peer_code, structures):
                op_code = summary["op_code"]
                record.unit.handle_on_receive(
                    op_code,
                    self._make_receive_callback(
                        record, unit_name, peer_code, op_code, summary["name"],
                        summary.get("namespace"),
                    ),
                    unit_name=unit_name,
                )

    def _make_receive_callback(self, record: ConnectionRecord, unit_name: str, peer_code: int,
                               op_code: int, message_name: str,
                               namespace: str | None = None) -> Callable[[Any], None]:
        def _on_message(message: Any) -> None:
            # Runs on a core executor thread. Must not raise: core logs and
            # swallows, but a clean log entry is more useful than a traceback.
            try:
                payload = message.to_dict() if hasattr(message, "to_dict") else {"raw": repr(message)}
                error = None
            except Exception as exc:  # noqa: BLE001
                payload, error = None, f"{type(exc).__name__}: {exc}"
            self._log(record, "received", unit_name, peer_code, op_code, message_name,
                      payload, error, namespace=namespace)
        return _on_message

    def _log(self, record: ConnectionRecord, direction: str, unit_name: str, unit_code: int,
             op_code: int, message_name: str, payload: dict[str, Any] | None,
             error: str | None, namespace: str | None = None) -> LogEntry:
        """Record one message and stream it. Every message, unconditionally.

        There was briefly a per-route rate limit here, and it was a mistake
        worth recording: capping the stream at 60Hz made consecutive console
        rows land ~16.7ms apart, which is exactly what the ORIGINAL scheduling
        bug looked like. The console is the instrument this whole area is
        diagnosed with, and an instrument that reports the sampling period
        instead of the signal is worse than a slow one. Volume is handled by
        batching in `api/routes/events.py`, where it costs no fidelity.
        """
        entry = LogEntry(
            seq=next(self._seq),
            direction=direction,
            connection_name=record.name,
            unit_name=unit_name,
            unit_code=unit_code,
            op_code=op_code,
            message_name=message_name,
            payload=payload,
            error=error,
            timestamp=wall_time(),
            namespace=namespace,
        )
        (record.sent if direction == "sent" else record.received).append(entry)
        self.events.publish({"type": f"message.{direction}", "entry": entry.as_dict()})
        return entry

    def _publish_state(self, record: ConnectionRecord) -> None:
        self.events.publish({"type": "connection.state", "connection": record.as_dict()})


_runtime: GSimRuntime | None = None


def get_runtime() -> GSimRuntime:
    global _runtime
    if _runtime is None:
        _runtime = GSimRuntime()
    return _runtime
