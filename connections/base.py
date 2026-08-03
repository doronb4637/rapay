"""
Core abstractions shared by every protocol implementation:

  * `_EventLoopThread` -- the single sync <-> async bridge for the whole
    process (see its docstring for the rationale).
  * `Connection` -- the ABC every protocol (TCP/UDP/Multicast/DDS) implements.
    It owns unit-name/unit-code resolution, the echo lifecycle (periodic
    sending + liveness watchdog), subscribe-or-drop message filtering,
    on-receive callbacks, periodic application sends, task tracking for
    absolute teardown, and the sync-facing public API, so concrete subclasses
    only ever have to write async code plus a single call into
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
from typing import Any, Callable, Coroutine

from .config import ConnectionConfig, EchoSettings

logger = logging.getLogger("connmgr")

#: Every routing table in this module is keyed by (unit_code, opcode) -- the
#: *numeric* unit code that actually travels in the wire header, never the
#: human-facing unit name. Names are a configuration-level convenience; the
#: code is the identity the protocol itself uses, so keying on it means the
#: routing tables and the bytes on the wire can never disagree.
RouteKey = tuple[int, int]

#: A no-argument callable run to solicit the message a receive_message() call
#: is waiting for -- see `Connection.receive_message`.
TriggerFunction = Callable[[], Any]

#: Invoked with one message payload per matching inbound message -- see
#: `Connection.handle_on_receive`.
ReceiveCallback = Callable[[bytes], Any]


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
    filtering, on-receive callbacks, periodic sending, the sync/async
    marshalling, and absolute-teardown task bookkeeping -- lives here so
    subclasses stay small and protocol-specific.
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

        # unit name -> numeric unit code. Snapshotted from the (immutable)
        # config once, because `_dispatch_incoming` sits on the hot path and
        # should never walk the connections table per message.
        self._unit_codes: dict[str, int] = config.unit_codes

        # Subscribe-or-drop delivery model (see _dispatch_incoming): keyed by
        # (unit_code, opcode) -> the single asyncio.Future belonging to the
        # in-flight receive_message() call waiting for exactly that pair. A
        # given route is subscribed by at most one caller at a time, so this
        # is one future, not a queue of them. Nothing is buffered here on
        # spec -- if there's no waiter when a message arrives,
        # _dispatch_incoming discards it immediately.
        self._subscriptions: dict[RouteKey, asyncio.Future[bytes]] = {}

        # Standing on-receive callbacks registered by handle_on_receive(),
        # keyed by the same route key. Where a subscription is consumed by
        # the first matching message, a callback stays until removed.
        self._callbacks: dict[RouteKey, ReceiveCallback] = {}

        # Background repeat-senders started by periodic_sending(), keyed by
        # the same route key: one repeating send per route, so starting a
        # second one replaces the first.
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
    # Unit resolution
    # ------------------------------------------------------------------ #
    def _unit_code_for(self, unit_name: str) -> int:
        code = self._unit_codes.get(unit_name)
        if code is None:
            raise ValueError(
                f"no unit code for unit {unit_name!r}; known units: {list(self._unit_codes)}"
            )
        return code

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
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """
        Boot this connection's async machinery on the shared background loop
        and block until it is actually listening / connected.

        Idempotent: calling it on an already-started connection is a no-op
        rather than a second set of sockets. The echo machinery is armed only
        after `_do_start()` returns, so the first heartbeat can never race the
        transport into existence.
        """
        if self._started:
            return
        self._loop_thread.run_coro(self._startup_all())
        self._started = True

    async def _startup_all(self) -> None:
        await self._do_start()
        self._start_echo_machinery()

    def stop(self, timeout: float | int | None = 5.0) -> None:
        """
        Sync entrypoint for an ABSOLUTE teardown:

          1. `_do_stop()` closes every socket/transport/server this
             connection owns, so no new inbound connection or datagram can
             ever be accepted again.
          2. Every background task this connection ever spawned via
             `_track()` (read loops, echo senders, echo watchdogs, periodic
             senders, in-flight on-receive callbacks) is explicitly cancelled
             and *awaited*, so nothing is left running on the shared loop
             thread after this call returns.
          3. Any receive_message() call still parked waiting on a
             subscription is released (its future is cancelled) instead of
             being left to hang forever, and every standing callback is
             dropped.

        Idempotent, and safe to call on a connection that was never started.
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
        self._callbacks.clear()

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
        connection's read loop, or an on-receive callback) and register it
        so `stop()` can guarantee it's cancelled and awaited -- no orphaned
        tasks, ever."""
        task = self._loop_thread.loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

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
        """Transmit the echo opcode to `unit_name` every EchoInterval, for as
        long as the connection lives. Unconditional by design: this is the
        only thing keeping the remote watchdog quiet, so it must not depend
        on having received anything."""
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
                # yet. If it stays unreachable, the watchdog is the thing that
                # reacts -- this loop just keeps trying.
                logger.warning("echo send to unit %s failed: %s", unit_name, exc)

    async def _echo_watchdog_loop(self, unit_name: str) -> None:
        """
        Disconnect `unit_name` once EchoTimeout seconds have passed with no
        echo from it.

        Rather than polling on a fixed tick, each iteration sleeps until this
        unit's *deadline* -- `last echo + EchoTimeout`. Waking any earlier
        only to find the deadline hasn't passed is wasted work, and waking on
        a fixed EchoTimeout tick instead would push worst-case detection out
        to nearly 2x EchoTimeout (an echo landing just before a tick resets
        the clock, but the next check is still a full timeout away). Sleeping
        to the deadline wakes at most once per timeout period AND detects the
        death at the deadline itself.

        Each pass re-reads `_last_echo_at`, so an echo arriving mid-sleep
        simply pushes the deadline out and the next pass sleeps again.
        """
        while True:
            last_seen = self._last_echo_at.get(unit_name)
            if last_seen is None:
                # Unit already gone (disconnected by another path).
                return
            remaining = (last_seen + self._echo_timeout) - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue  # re-check: an echo may have moved the deadline
            elapsed = time.monotonic() - last_seen
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
        for route_key in [key for key in self._periodic_tasks if key[0] == unit_code]:
            task = self._periodic_tasks.pop(route_key)
            if not task.done():
                task.cancel()

        # 2. Fail any parked receive_message() for that unit right away
        #    rather than making the caller sit out its full timeout on a link
        #    we already know is dead, and drop its standing callbacks -- they
        #    can never fire again.
        for route_key in [key for key in self._subscriptions if key[0] == unit_code]:
            future = self._subscriptions.pop(route_key)
            if not future.done():
                future.set_exception(
                    ConnectionError(f"unit {unit_name!r} disconnected: echo timeout")
                )
        for route_key in [key for key in self._callbacks if key[0] == unit_code]:
            del self._callbacks[route_key]

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
    # Incoming message dispatch
    # ------------------------------------------------------------------ #
    def _dispatch_incoming(self, unit_name: str, opcode: int, payload: bytes) -> None:
        """
        Called by a subclass's read loop / datagram callback (always on the
        shared event-loop thread) whenever a complete inbound message has
        been parsed. This is the single choke point implementing:

          1. Echo consumption -- if `opcode` matches the configured "receive
             echo" opcode, the message is consumed here: it refreshes this
             unit's liveness timestamp, satisfying the watchdog, and goes no
             further. It is never visible to the application, no matter what
             is or isn't subscribed. No reply is sent: the periodic sender
             already transmits on its own schedule, so answering an inbound
             echo would be pure duplication -- and with a single shared
             echo_opcode it would have both peers answering each other's
             answers without end.
          2. Subscribe-or-drop filtering -- otherwise, the message goes to
             whoever owns this exact (unit_code, opcode) route: a parked
             receive_message() future if one is in flight, else a standing
             handle_on_receive() callback. If neither exists, the message is
             discarded immediately; it is never buffered for a hypothetical
             future call.
        """
        if self._echo_enabled and opcode == self._recv_echo_opcode:
            self._last_echo_at[unit_name] = time.monotonic()
            return

        route_key: RouteKey = (self._unit_code_for(unit_name), opcode)

        # A one-shot subscription wins: the caller is actively blocked on it.
        # By construction (see _subscribe / _register_callback) a route never
        # has both a subscription and a callback at the same time.
        future = self._subscriptions.get(route_key)
        if future is not None and not future.done():
            del self._subscriptions[route_key]
            future.set_result(payload)
            return

        callback = self._callbacks.get(route_key)
        if callback is not None:
            self._track(self._run_callback(callback, payload, unit_name, opcode))
            return
        # Nobody owns this route right now -- drop it.

    async def _run_callback(
        self, callback: ReceiveCallback, payload: bytes, unit_name: str, opcode: int
    ) -> None:
        """
        Run one on-receive callback off the event-loop thread.

        Callbacks are ordinary synchronous user code: they may block, and
        they may call back into this connection's sync API (`send_message`,
        `receive_message`). Running them inline on the loop thread would
        stall every other connection in the process, and any sync API call
        from there would deadlock -- that call marshals onto the loop thread
        and waits for it, but the loop thread is what's running the callback.
        Handing them to the default executor avoids both.
        """
        try:
            await self._loop_thread.loop.run_in_executor(None, callback, payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad callback must not kill the read loop
            logger.exception(
                "on-receive callback for unit %s opcode %s raised", unit_name, opcode
            )

    # ------------------------------------------------------------------ #
    # Subscription internals (all run on the loop thread)
    # ------------------------------------------------------------------ #
    async def _subscribe(self, route_key: RouteKey) -> asyncio.Future[bytes]:
        """
        Register the future that makes this route "subscribed", and return it
        without awaiting.

        Split out from the await so `receive_message` can run a trigger
        function in between: by the time the trigger fires, the future is
        already in `_subscriptions`, so a reply that arrives immediately --
        even before the caller gets as far as awaiting -- is captured by the
        future rather than dropped as unsubscribed.
        """
        if route_key in self._callbacks:
            raise RuntimeError(
                f"route (unit_code={route_key[0]}, opcode={route_key[1]}) already has an "
                f"on-receive callback; call stop_on_receive() before receive_message()"
            )
        existing = self._subscriptions.get(route_key)
        if existing is not None and not existing.done():
            # One subscriber per route, by design. Silently replacing the
            # earlier future would strand that caller forever, so say so.
            raise RuntimeError(
                f"already subscribed to (unit_code={route_key[0]}, opcode={route_key[1]}): "
                f"only one receive_message() may be in flight per route"
            )

        # Deliberately the explicitly-tracked loop rather than
        # asyncio.get_running_loop(): this coroutine's future outlives it and
        # is awaited by a *different* coroutine (_await_subscription), so the
        # future's loop must be the connection's loop as a matter of record,
        # not merely whichever loop happened to be running at this instant.
        # They are the same loop today -- everything here is marshalled
        # through _loop_thread -- which is exactly why naming it explicitly
        # costs nothing and documents the invariant.
        future: asyncio.Future[bytes] = self._loop_thread.loop.create_future()
        self._subscriptions[route_key] = future
        return future

    async def _await_subscription(
        self, route_key: RouteKey, future: asyncio.Future[bytes], timeout: float | int | None
    ) -> bytes:
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
            if self._subscriptions.get(route_key) is future:
                del self._subscriptions[route_key]

    async def _unsubscribe(self, route_key: RouteKey, future: asyncio.Future[bytes]) -> None:
        """Release a subscription that will never be awaited (the trigger
        function raised), so the route doesn't stay claimed."""
        if self._subscriptions.get(route_key) is future:
            del self._subscriptions[route_key]
        if not future.done():
            future.cancel()

    # ------------------------------------------------------------------ #
    # Public sync API
    # ------------------------------------------------------------------ #
    def send_message(self, data: bytes, opcode: int, unit_name: str | None = None) -> None:
        """
        Send `data` tagged with `opcode` to `unit_name` (or the sole
        connected unit if omitted). `opcode` is mandatory: every message
        must declare what kind of message it is so the receiving side's
        subscribe-or-drop filtering and echo handling can work.
        """
        unit = self._resolve_unit(unit_name)
        self._loop_thread.run_coro(self._do_send(unit, data, opcode))

    def receive_message(
        self,
        opcode: int,
        unit_name: str | None = None,
        timeout: float | int | None = None,
        trigger_function: TriggerFunction | None = None,
    ) -> tuple[str, bytes]:
        """
        Blocking, synchronous receive. Returns (unit_name, payload) for the
        first message matching BOTH `opcode` and the resolved unit.

        Calling this function is what "subscribes" the connection to that
        exact route -- nothing is delivered, and nothing is buffered, until a
        matching receive_message() call is in flight. Only one call may be
        subscribed to a given (unit, opcode) at a time, and a route that has
        a standing `handle_on_receive` callback cannot also be polled.

        If `unit_name` is omitted and this connection has exactly one
        connected unit (per `config.connections`), that unit is used
        automatically; otherwise `unit_name` must be given explicitly.

        `trigger_function` is for the request/response case: pass the call
        that *solicits* the message being waited for (typically a
        `send_message`) and it runs AFTER the subscription is registered but
        BEFORE this call starts blocking. That ordering is the whole point --
        soliciting first and subscribing second would drop a reply that beat
        the subscription into place, which under subscribe-or-drop is gone
        for good. It runs in the caller's own thread, so it may freely use
        this connection's sync API, and if it raises, the subscription is
        released and the exception propagates unchanged.
        """
        unit, route_key = self._resolve_route(unit_name, opcode)

        # Step 1: arm the subscription. Returns as soon as the future is in
        # the table -- it does not wait for a message.
        future: asyncio.Future[bytes] = self._loop_thread.run_coro(self._subscribe(route_key))

        # Step 2: solicit the message, now that we are guaranteed to catch it.
        if trigger_function is not None:
            try:
                trigger_function()
            except BaseException:
                self._loop_thread.run_coro(self._unsubscribe(route_key, future))
                raise

        # Step 3: block until it lands. The outer timeout is deliberately
        # looser than the inner one so the inner asyncio.wait_for is what
        # actually fires, giving callers a clean TimeoutError instead of a
        # bridge-level one.
        wait_timeout = timeout + 1 if timeout is not None else None
        payload: bytes = self._loop_thread.run_coro(
            self._await_subscription(route_key, future, timeout), timeout=wait_timeout
        )
        return unit, payload

    def handle_on_receive(
        self,
        opcode: int,
        callback_func: ReceiveCallback,
        unit_name: str | None = None,
    ) -> None:
        """
        Register a standing handler for a route instead of polling it.

        Where `receive_message` subscribes, takes ONE message and unsubscribes,
        this registers `callback_func` permanently: every subsequent inbound
        message matching `opcode` and the resolved unit invokes
        `callback_func(payload)`, until `stop_on_receive()` or `stop()`.
        Messages arriving while a callback is still running are dispatched
        too, so a slow callback may run concurrently with itself -- keep it
        cheap, or serialize inside it.

        The callback runs on an executor thread, never on the event loop, so
        it is free to block and to call back into this connection's sync API
        (`send_message`, `receive_message` on some other route). An exception
        raised inside it is logged and swallowed rather than killing the read
        loop.

        A route can have a callback or an in-flight `receive_message`, never
        both -- registering over either raises `RuntimeError` rather than
        silently deciding which one wins.
        """
        if not callable(callback_func):
            raise TypeError(f"callback_func must be callable, got {callback_func!r}")
        _unit, route_key = self._resolve_route(unit_name, opcode)
        self._loop_thread.run_coro(self._register_callback(route_key, callback_func))

    def stop_on_receive(self, opcode: int, unit_name: str | None = None) -> bool:
        """
        Remove the standing callback registered for this route by
        `handle_on_receive`. Returns True if one was registered, False if
        there was nothing to remove. Callbacks already running are left to
        finish.
        """
        _unit, route_key = self._resolve_route(unit_name, opcode)
        removed: bool = self._loop_thread.run_coro(self._unregister_callback(route_key))
        return removed

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

    # -- callback internals (all run on the loop thread) ------------------ #
    async def _register_callback(self, route_key: RouteKey, callback: ReceiveCallback) -> None:
        existing = self._subscriptions.get(route_key)
        if existing is not None and not existing.done():
            raise RuntimeError(
                f"route (unit_code={route_key[0]}, opcode={route_key[1]}) has a "
                f"receive_message() in flight; a route cannot be polled and handled at once"
            )
        if route_key in self._callbacks:
            raise RuntimeError(
                f"route (unit_code={route_key[0]}, opcode={route_key[1]}) already has an "
                f"on-receive callback; call stop_on_receive() first"
            )
        self._callbacks[route_key] = callback

    async def _unregister_callback(self, route_key: RouteKey) -> bool:
        return self._callbacks.pop(route_key, None) is not None

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
        """Open every socket/transport/server this connection owns. Must not
        return until inbound traffic can actually be accepted."""
        ...

    @abstractmethod
    async def _do_stop(self) -> None:
        """Close everything `_do_start()` opened. Called first during
        teardown, before any task cancellation, so no read loop can be woken
        by traffic while it is being cancelled."""
        ...

    @abstractmethod
    async def _do_send(self, unit_name: str, data: bytes, opcode: int) -> None:
        """Frame (if applicable) and transmit one message to `unit_name`.
        Raise `ConnectionError` if the unit currently has no usable peer --
        the periodic and echo senders treat that as retryable."""
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
        """Build the wire bytes for one message: the 5-byte header carrying
        this unit's code, the opcode and the payload length, then the payload
        itself."""
        from .framing import pack_message
        return pack_message(self._unit_code_for(unit_name), opcode, data)
