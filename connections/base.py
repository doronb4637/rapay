"""
Core abstractions shared by every protocol implementation:

  * `_EventLoopThread` -- the single sync <-> async bridge for the whole
    process (see its docstring for the rationale).
  * `Connection` -- the ABC every protocol (TCP/UDP/Multicast/DDS) implements.
    It owns unit-name/unit-code resolution, the echo lifecycle (auto-reply,
    periodic sending, liveness timeout), subscribe-or-drop message filtering,
    periodic application sends, task tracking for absolute teardown, and the
    sync-facing start/stop/send_message/receive_message surface, so concrete
    subclasses only ever have to write async code plus a single call into
    `_dispatch_incoming()` per parsed message.
"""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Coroutine

from .config import ConnectionConfig, EchoSettings

logger = logging.getLogger("connmgr")

#: Every routing table in this module is keyed by (unit_code, opcode) -- the
#: *numeric* unit code that actually travels in the wire header, never the
#: human-facing unit name. Names are a configuration-level convenience; the
#: code is the identity the protocol itself uses, so keying on it means the
#: subscription table and the bytes on the wire can never disagree.
RouteKey = tuple[int, int]


class _EventLoopThread:
    """
    Owns exactly ONE background thread running an asyncio event loop for the
    entire process -- this is the sync <-> async bridge:

      - All actual socket I/O (asyncio streams / transports / protocols)
        runs as coroutines *inside* this loop: cheap, single-threaded,
        non-blocking, and scales to many connections without needing one OS
        thread per connection (which is what naive `threading`-based designs
        end up doing).
      - The public, synchronous API (`Connection.start/.stop/.send_message`,
        etc.) is called from the caller's ordinary synchronous thread. Each
        call is marshalled onto the loop thread with
        `asyncio.run_coroutine_threadsafe(...)` and blocks the *calling*
        thread only until that specific coroutine finishes. The caller never
        has to know asyncio exists.

    This is a lazily-created singleton: one loop/thread services every
    Connection instance the process creates, so we don't pay per-connection
    thread overhead. `threading` is used here for exactly one thing --
    hosting the event loop -- never for I/O itself.
    """
    _instance: _EventLoopThread | None = None
    _lock = threading.Lock()

    def __new__(cls) -> _EventLoopThread:
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._start()
                cls._instance = inst
                atexit.register(inst.shutdown)
            return cls._instance

    def _start(self) -> None:
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()

        self._thread = threading.Thread(target=_run, name="connection-mgr-event-loop", daemon=True)
        self._thread.start()
        ready.wait()

    def run_coro(self, coro: Coroutine[Any, Any, Any], timeout: float | int | None = None) -> Any:
        """Submit a coroutine to the loop thread and block the CALLER (a normal
        sync thread) until it completes. This is the workhorse of the bridge."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        """Non-blocking submission for fire-and-forget style calls."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def shutdown(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


def get_event_loop_thread() -> _EventLoopThread:
    return _EventLoopThread()


class Connection(ABC):
    """
    Unified interface for TCP, UDP, Multicast and DDS connections.

    Concrete subclasses implement three async hooks:
        _do_start()                            -- open sockets/transports
        _do_stop()                             -- close them (no new I/O accepted)
        _do_send(unit_name, data, opcode)      -- send framed/native payload
    and call `self._dispatch_incoming(unit_name, opcode, payload)` from
    wherever their read-loop / datagram callback lives, once per fully
    parsed inbound message. They may additionally override
    `_do_disconnect_unit()` so the echo-timeout watchdog can drop a single
    dead unit without touching the healthy ones.

    Everything else -- unit resolution, the echo lifecycle, subscribe-or-drop
    filtering, periodic sending, the sync/async marshalling, and
    absolute-teardown task bookkeeping -- lives here so subclasses stay
    small and protocol-specific.
    """

    #: Whether this connection instance is capable of sending / receiving.
    #: Overridden by e.g. a send-only Multicast connection. Used by
    #: CompositeUnit to combine one-way connections (see composite.py).
    can_send: bool = True
    can_receive: bool = True

    def __init__(self, config: ConnectionConfig) -> None:
        self.config = config
        self._loop_thread = get_event_loop_thread()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._started = False

        # unit name -> numeric unit code, resolved and validated once at
        # construction. Every routing table below keys off the code, and
        # `_dispatch_incoming` sits on the hot path, so we never want to be
        # walking `unit_map` per message to work it out.
        self._unit_codes: dict[str, int] = self._build_unit_codes()

        # Subscribe-or-drop delivery model (see _dispatch_incoming): keyed by
        # (unit_code, opcode) -> the single asyncio.Future belonging to the
        # in-flight receive_message() call waiting for exactly that pair. A
        # given (unit, opcode) is subscribed by at most one caller at a time,
        # so this is one future, not a queue of them. Nothing is buffered here
        # on spec -- if there's no waiter when a message arrives,
        # _dispatch_incoming discards it immediately.
        self._subscriptions: dict[RouteKey, asyncio.Future[bytes]] = {}

        # Background repeat-senders started by periodic_sending(), keyed by
        # the same (unit_code, opcode) route key: one repeating send per
        # route, so starting a second one replaces the first.
        self._periodic_tasks: dict[RouteKey, asyncio.Task[None]] = {}

        # Echo lifecycle, parsed from config.extra (see config.EchoSettings).
        # Inactive unless BOTH opcodes resolve -- via a single "echo_opcode"
        # or an explicit recv/send pair.
        self._echo: EchoSettings = config.echo
        self._recv_echo_opcode: int | None = self._echo.recv_opcode
        self._send_echo_opcode: int | None = self._echo.send_opcode
        self._echo_enabled: bool = self._echo.enabled
        self._echo_interval: float = self._echo.interval
        self._echo_timeout: float = self._echo.timeout
        #: unit name -> time.monotonic() of the last echo received from it.
        self._last_echo_at: dict[str, float] = {}
        #: unit name -> its echo sender / watchdog tasks.
        self._echo_tasks: dict[str, list[asyncio.Task[None]]] = {}

    # ------------------------------------------------------------------ #
    # Unit codes
    # ------------------------------------------------------------------ #
    def _build_unit_codes(self) -> dict[str, int]:
        """
        Resolve every configured unit name to the numeric unit code used in
        the wire header and in every routing key.

        Codes come from `config.extra["unit_codes"]` when given, otherwise
        default to the low byte of the unit's port -- simple and
        deterministic, but only unambiguous if the resulting codes are
        distinct, which is exactly what this method enforces. Two units
        sharing a code would collapse into one subscription slot and silently
        deliver one unit's traffic to the other's waiter, so it is rejected
        here rather than debugged later.
        """
        explicit: dict[str, Any] = self.config.extra.get("unit_codes") or {}
        codes: dict[str, int] = {}
        code_owner: dict[int, str] = {}

        for port, unit_name in self.config.unit_map.items():
            if unit_name in codes:
                raise ValueError(
                    f"unit {unit_name!r} is mapped to more than one port in config['units']; "
                    f"each unit name must identify exactly one port"
                )
            if unit_name in explicit:
                code = int(explicit[unit_name])
            else:
                code = port & 0xFF
            if not 0 <= code <= 0xFF:
                raise ValueError(
                    f"unit code {code} for {unit_name!r} does not fit in the uint8 UnitCode field"
                )
            if code in code_owner:
                raise ValueError(
                    f"units {code_owner[code]!r} and {unit_name!r} both resolve to unit code "
                    f"{code}; give them distinct codes via config['unit_codes']"
                )
            codes[unit_name] = code
            code_owner[code] = unit_name

        return codes

    def _unit_code_for(self, unit_name: str) -> int:
        code = self._unit_codes.get(unit_name)
        if code is None:
            raise ValueError(
                f"no unit code for unit {unit_name!r}; known units: {list(self._unit_codes)}"
            )
        return code

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Sync entrypoint: boots this connection's async machinery on the
        shared background loop and blocks until it's actually listening /
        connected."""
        if self._started:
            return
        self._loop_thread.run_coro(self._startup_all())
        self._started = True

    async def _startup_all(self) -> None:
        await self._do_start()
        # Only once the transport is actually up, so the first echo can't
        # race the socket into existence.
        self._start_echo_machinery()

    def stop(self, timeout: float | int | None = 5.0) -> None:
        """
        Sync entrypoint for an ABSOLUTE teardown:

          1. `_do_stop()` closes every socket/transport/server this
             connection owns, so no new inbound connection or datagram can
             ever be accepted again.
          2. Every background task this connection ever spawned via
             `_track()` (read loops, echo senders, echo watchdogs, periodic
             senders, auto-echo replies) is explicitly cancelled and
             *awaited*, so nothing is left running on the shared loop thread
             after this call returns.
          3. Any receive_message() call still parked waiting on a
             subscription is released (its future is cancelled) instead of
             being left to hang forever.
        """
        if not self._started:
            return
        self._loop_thread.run_coro(self._shutdown_all(), timeout=timeout)
        self._started = False

    async def _shutdown_all(self) -> None:
        await self._do_stop()
        # _periodic_tasks / _echo_tasks hold the same Task objects that
        # _track() registered in _tasks, so cancelling _tasks covers them
        # all; the dedicated dicts are just cleared of their stale entries.
        self._periodic_tasks.clear()
        self._echo_tasks.clear()
        self._last_echo_at.clear()

        pending = [t for t in self._tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

        for future in self._subscriptions.values():
            if not future.done():
                future.cancel()
        self._subscriptions.clear()

    def _track(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Subclass helper: schedule a background coroutine (e.g. a
        connection's read loop, or an automatic echo reply) and register it
        so `stop()` can guarantee it's cancelled and awaited -- no orphaned
        tasks, ever."""
        task = self._loop_thread.loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ #
    # Unit resolution
    # ------------------------------------------------------------------ #
    def _resolve_unit(self, unit_name: str | None) -> str:
        units = self.config.connected_units
        if unit_name is not None:
            if unit_name not in units:
                raise ValueError(f"Unknown unit {unit_name!r}; known units: {units}")
            return unit_name
        if len(units) == 1:
            return units[0]
        raise ValueError(
            f"unit_name is required: this connection has multiple connected "
            f"units {units}"
        )

    def _resolve_route(self, unit_name: str | None, opcode: int) -> tuple[str, RouteKey]:
        """Resolve the caller's optional unit name into both the name (for
        the protocol layer and for returning to the caller) and the
        (unit_code, opcode) key every internal table is keyed by."""
        unit = self._resolve_unit(unit_name)
        return unit, (self._unit_code_for(unit), opcode)

    # ------------------------------------------------------------------ #
    # Echo lifecycle: periodic sending + liveness watchdog
    # ------------------------------------------------------------------ #
    def _start_echo_machinery(self) -> None:
        """
        Spin up, per connected unit:

          * an echo SENDER -- transmits the echo opcode every EchoInterval
            seconds unconditionally, whether or not anything was received.
            This is what makes the peer's own watchdog stay quiet.
          * an echo WATCHDOG -- drops that unit if no echo has arrived from
            it within EchoTimeout seconds.

        Direction-limited connections only get the half they can actually
        perform: a send-only member has no way to receive an echo, so
        watchdogging it would guarantee a spurious disconnect.
        """
        if not self._echo_enabled:
            return
        now = time.monotonic()
        for unit_name in self.config.connected_units:
            # Seed liveness at start-up, otherwise the watchdog would fire
            # EchoTimeout after the epoch, i.e. instantly.
            self._last_echo_at[unit_name] = now
            unit_tasks: list[asyncio.Task[None]] = []
            if self.can_send:
                unit_tasks.append(self._track(self._echo_sender_loop(unit_name)))
            if self.can_receive:
                unit_tasks.append(self._track(self._echo_watchdog_loop(unit_name)))
            self._echo_tasks[unit_name] = unit_tasks

    async def _echo_sender_loop(self, unit_name: str) -> None:
        assert self._send_echo_opcode is not None
        while True:
            # Sleep first: _do_start() has returned, but a TCP server may not
            # have an accepted peer yet, and there is no value in an echo at
            # t=0 anyway.
            await asyncio.sleep(self._echo_interval)
            try:
                await self._do_send(unit_name, self._echo.payload, self._send_echo_opcode)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed echo must not kill the loop
                # Deliberately non-fatal: the peer may simply not be connected
                # yet. If it stays unreachable, the peer's own watchdog is the
                # thing that reacts -- this loop just keeps trying.
                logger.warning("echo send to unit %s failed: %s", unit_name, exc)

    async def _echo_watchdog_loop(self, unit_name: str) -> None:
        # Poll finer than the timeout so detection latency is a fraction of
        # EchoTimeout rather than a whole extra period of it.
        poll_interval = max(min(self._echo_interval, self._echo_timeout) / 2.0, 0.05)
        while True:
            await asyncio.sleep(poll_interval)
            last_seen = self._last_echo_at.get(unit_name)
            if last_seen is None:
                continue
            elapsed = time.monotonic() - last_seen
            if elapsed < self._echo_timeout:
                continue
            logger.warning(
                "no echo from unit %s for %.2fs (EchoTimeout=%.2fs) -- disconnecting it",
                unit_name, elapsed, self._echo_timeout,
            )
            await self._disconnect_unit(unit_name)
            return  # this unit is gone; nothing left for the watchdog to watch

    async def _disconnect_unit(self, unit_name: str) -> None:
        """
        Tear down one unit's transport without disturbing the others, and
        make sure nothing is left waiting on a link that is now dead.
        """
        unit_code = self._unit_codes.get(unit_name)

        # 1. Stop repeating sends aimed at the dead unit -- they would only
        #    log a failure once per interval, forever.
        for route_key in [k for k in self._periodic_tasks if k[0] == unit_code]:
            task = self._periodic_tasks.pop(route_key)
            if not task.done():
                task.cancel()

        # 2. Fail any parked receive_message() for that unit right away
        #    rather than making the caller sit out its full timeout on a link
        #    we already know is dead.
        for route_key in [k for k in self._subscriptions if k[0] == unit_code]:
            future = self._subscriptions.pop(route_key)
            if not future.done():
                future.set_exception(
                    ConnectionError(f"unit {unit_name!r} disconnected: echo timeout")
                )

        # 3. Stop echoing at it. The watchdog calling us is itself in this
        #    list and is about to return on its own, so skip cancelling it.
        current = asyncio.current_task()
        for task in self._echo_tasks.pop(unit_name, []):
            if task is not current and not task.done():
                task.cancel()
        self._last_echo_at.pop(unit_name, None)

        # 4. Finally close the protocol-level resources for this unit.
        try:
            await self._do_disconnect_unit(unit_name)
        except Exception:  # noqa: BLE001 - teardown must not raise into the watchdog
            logger.exception("error disconnecting unit %s", unit_name)

    # ------------------------------------------------------------------ #
    # Incoming message dispatch: automatic echo handling + subscribe-or-drop
    # ------------------------------------------------------------------ #
    def _dispatch_incoming(self, unit_name: str, opcode: int, payload: bytes) -> None:
        """
        Called by a subclass's read loop / datagram callback (always on the
        shared event-loop thread) whenever a complete inbound message has
        been parsed. This is the single choke point implementing:

          1. Automatic echo handling -- if `opcode` matches the configured
             "receive echo" opcode, the message is consumed here: it refreshes
             this unit's liveness timestamp (satisfying the watchdog) and, in
             the request/reply configuration, fires off the reply itself. The
             message never reaches the public API / any receive_message()
             caller, no matter what is or isn't subscribed.
          2. Subscribe-or-drop filtering -- otherwise, the message is
             delivered ONLY if some in-flight receive_message() call is
             currently subscribed to this exact (unit_code, opcode) route.
             If nothing is subscribed, the message is discarded immediately;
             it is never buffered for a hypothetical future call.
        """
        if self._echo_enabled and opcode == self._recv_echo_opcode:
            self._last_echo_at[unit_name] = time.monotonic()
            # Symmetric heartbeat (one shared echo_opcode): the periodic
            # sender already answers for us, and replying here would have both
            # peers answering each other's answers without end. See
            # EchoSettings.single_opcode.
            if not self._echo.single_opcode:
                assert self._send_echo_opcode is not None
                self._track(self._do_send(unit_name, payload, self._send_echo_opcode))
            return

        sub_key: RouteKey = (self._unit_code_for(unit_name), opcode)
        future = self._subscriptions.get(sub_key)
        if future is None or future.done():
            return  # nobody subscribed to this route right now -- drop it
        # Consume the subscription: it belongs to exactly one receive_message()
        # call and is satisfied now.
        del self._subscriptions[sub_key]
        future.set_result(payload)

    async def _wait_for_message(self, sub_key: RouteKey, timeout: float | int | None) -> bytes:
        existing = self._subscriptions.get(sub_key)
        if existing is not None and not existing.done():
            # One subscriber per route, by design. Silently replacing the
            # earlier future would strand that caller forever, so say so.
            raise RuntimeError(
                f"already subscribed to (unit_code={sub_key[0]}, opcode={sub_key[1]}): "
                f"only one receive_message() may be in flight per route"
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        # Registering the future here IS "subscribing" -- _dispatch_incoming
        # only ever delivers a message if it finds a future under this key.
        self._subscriptions[sub_key] = future
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            # Unsubscribe on the way out -- whether we got a message, timed
            # out, or were cancelled -- so an abandoned subscription never
            # lingers and silently swallows a later message. The identity
            # check matters: on the delivery path _dispatch_incoming already
            # removed this future, and a later subscriber may have claimed
            # the slot in the meantime; we must only ever remove our own.
            if self._subscriptions.get(sub_key) is future:
                del self._subscriptions[sub_key]

    # ------------------------------------------------------------------ #
    # Public sync API
    # ------------------------------------------------------------------ #
    def send_message(self, data: bytes, opcode: int, unit_name: str | None = None) -> None:
        """
        Send `data` tagged with `opcode` to `unit_name` (or the sole
        connected unit if omitted). `opcode` is mandatory: every message
        must declare what kind of message it is so the receiving side's
        subscribe-or-drop filtering and automatic echo handling can work.
        """
        unit = self._resolve_unit(unit_name)
        self._loop_thread.run_coro(self._do_send(unit, data, opcode))

    def receive_message(
        self,
        opcode: int,
        unit_name: str | None = None,
        timeout: float | int | None = None,
    ) -> tuple[str, bytes]:
        """
        Blocking, synchronous receive. Returns (unit_name, payload) for the
        first message matching BOTH `opcode` and the resolved unit.

        Calling this function is what "subscribes" the connection to that
        exact route -- nothing is delivered, and nothing is buffered, until a
        matching receive_message() call is in flight. Only one call may be
        subscribed to a given (unit, opcode) at a time.

        If `unit_name` is omitted and this connection has exactly one
        connected unit (per `config.unit_map`), that unit is used
        automatically; otherwise `unit_name` must be given explicitly.
        """
        unit, sub_key = self._resolve_route(unit_name, opcode)
        # Outer timeout is deliberately looser than the inner one so the
        # inner asyncio.wait_for is what actually fires, giving callers a
        # clean TimeoutError instead of a bridge-level one.
        wait_timeout = timeout + 1 if timeout is not None else None
        payload: bytes = self._loop_thread.run_coro(
            self._wait_for_message(sub_key, timeout), timeout=wait_timeout
        )
        return unit, payload

    def periodic_sending(
        self,
        opcode: int,
        data: bytes,
        interval: int | float,
        unit_name: str | None = None,
    ) -> None:
        """
        Like `send_message`, but keeps sending `data` in the background every
        `interval` seconds until `stop_periodic` (or `stop`) is called.

        At most one periodic sender exists per (unit_code, opcode) route:
        calling this again for a route that already has one cancels and
        replaces the previous sender, so the send rate for a given route is
        whatever the most recent call asked for -- never two overlapping
        senders quietly doubling it.

        `unit_name` is optional when this connection has exactly one
        connected unit, matching `send_message`/`receive_message`.
        """
        interval_seconds = float(interval)
        if interval_seconds <= 0:
            raise ValueError(f"interval must be > 0 seconds, got {interval!r}")
        unit, route_key = self._resolve_route(unit_name, opcode)
        self._loop_thread.run_coro(
            self._start_periodic(unit, route_key, data, opcode, interval_seconds)
        )

    def stop_periodic(self, opcode: int, unit_name: str | None = None) -> bool:
        """
        Stop the periodic sender started for this (unit, opcode) route.
        Returns True if one was running, False if there was nothing to stop.
        """
        _unit, route_key = self._resolve_route(unit_name, opcode)
        stopped: bool = self._loop_thread.run_coro(self._stop_periodic(route_key))
        return stopped

    # -- periodic sending internals (all run on the loop thread) ---------- #
    async def _start_periodic(
        self,
        unit_name: str,
        route_key: RouteKey,
        data: bytes,
        opcode: int,
        interval: float,
    ) -> None:
        # Replace, don't stack: cancel and *await* the previous sender before
        # storing the new one, so the two can never overlap even for one tick.
        await self._stop_periodic(route_key)

        task = self._track(self._periodic_send_loop(unit_name, data, opcode, interval))
        self._periodic_tasks[route_key] = task

        # Keep the registry honest if the loop ever ends by itself; the
        # identity check avoids evicting a newer sender for the same route.
        def _forget(finished: asyncio.Task[Any], key: RouteKey = route_key) -> None:
            if self._periodic_tasks.get(key) is finished:
                del self._periodic_tasks[key]

        task.add_done_callback(_forget)

    async def _stop_periodic(self, route_key: RouteKey) -> bool:
        task = self._periodic_tasks.pop(route_key, None)
        if task is None:
            return False
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return True

    async def _periodic_send_loop(
        self, unit_name: str, data: bytes, opcode: int, interval: float
    ) -> None:
        while True:
            try:
                await self._do_send(unit_name, data, opcode)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad send must not end the schedule
                # A transient failure (peer not connected yet, socket busy)
                # shouldn't silently kill a periodic send the caller believes
                # is still running -- log it and try again next tick.
                logger.warning(
                    "periodic send (unit=%s, opcode=%s) failed: %s", unit_name, opcode, exc
                )
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ #
    # Subclass hooks
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def _do_start(self) -> None:
        ...

    @abstractmethod
    async def _do_stop(self) -> None:
        ...

    @abstractmethod
    async def _do_send(self, unit_name: str, data: bytes, opcode: int) -> None:
        ...

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """
        Close just this unit's transport, leaving every other unit on this
        connection running. Called by the echo watchdog on EchoTimeout.

        Subclasses that multiplex several units over separate sockets should
        override this. The default is a no-op so a protocol that genuinely
        cannot isolate one unit degrades to "log it and keep going" rather
        than tearing down unrelated links.
        """
        logger.warning(
            "%s does not implement per-unit disconnect; unit %s left open",
            type(self).__name__, unit_name,
        )


class FramedConnection(Connection):
    """
    Mixin adding the (UnitCode, OpCode, DataLength) header framing that TCP,
    UDP and Multicast connections require. DDS does NOT inherit from this --
    its payloads are handled natively.
    """

    def _frame(self, unit_name: str, data: bytes, opcode: int) -> bytes:
        from .framing import pack_message
        return pack_message(self._unit_code_for(unit_name), opcode, data)
