"""
Core abstractions shared by every protocol implementation:

  * `_EventLoopThread` -- the single sync <-> async bridge for the whole process.
  * `Connection` -- the ABC every protocol (TCP/UDP/Multicast/DDS) implements:
    unit resolution, per-unit connection state, subscribe-or-drop dispatch,
    callbacks, periodic sends, and task tracking for absolute teardown.
"""
from __future__ import annotations
import asyncio
import atexit
import concurrent.futures
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine, Iterable, Protocol

from core.annotations import IrsMessage, OpCode, UnitCode
from core.IRS import Message, is_definition
from core.IRS.irs_parser import Interface, IRSDataError, parse
from core.tools.general import extract_opcode

from ._echo import UnitEchoSupervisor
from ._routes import (ConnectCallback, MessageClass, MessageKey, Owner, ReceiveCallback, RouteKey,
                      RouteTable, UnitName, describe_key)
from .config import ConnectionConfig, EchoSettings, load_interfaces
from .framing import pack_message

logger = logging.getLogger("connmgr")

TriggerFunction = Callable[[], Any]
#: What a send takes: a `@Definition` message instance (it carries its own
#: route), raw bytes with an explicit opcode, or -- on DDS -- a typed sample.
Outbound = Message | bytes
ConnectedTarget = int | UnitName | Iterable[UnitName]
#: A periodic schedule's key. The unit is None for a DDS broadcast, which has no
#: single destination.
PeriodicKey = tuple[UnitCode | None, MessageKey]


class Unit(Protocol):
    """
    The public surface of a logical unit -- a single `Connection` or a
    `CompositeUnit` -- as `ConnectionManager.create` / `create_composite` return it.
    """

    @property
    def active_units(self) -> set[str]: ...
    def start(self, retry: bool = False) -> None: ...
    def close(self, timeout: float | int | None = 5.0) -> None: ...
    def send_message(self, data: Outbound, opcode: OpCode | None = None, unit_name: str | None = None) -> None: ...
    def receive_message(self, opcode: OpCode, unit_name: str | None = None,
                        timeout: float | int | None = None, trigger_function: TriggerFunction | None = None) -> IrsMessage: ...
    def handle_on_receive(self, opcode: OpCode, callback_func: ReceiveCallback, unit_name: str | None = None) -> None: ...
    def stop_on_receive(self, opcode: OpCode, unit_name: str | None = None) -> bool: ...
    def handle_on_connect(self, callback_func: ConnectCallback, unit_name: str | None = None) -> None: ...
    def stop_on_connect(self, unit_name: str | None = None) -> bool: ...
    def interface_for(self, unit_name: str | None = None) -> Interface: ...
    def periodic_sending(self, data: Outbound, interval: int | float,
                         opcode: OpCode | None = None, unit_name: str | None = None) -> None: ...
    def stop_periodic(self, opcode: OpCode,
                      unit_name: str | None = None) -> bool: ...
    def wait_for_connected_units(self, target: ConnectedTarget,
                                 timeout: float | int | None = None) -> bool: ...


class _EventLoopThread:
    """
    The process-wide singleton thread running the one asyncio loop every
    `Connection` does its I/O on. Sync callers marshal onto it, so the public
    API stays blocking and nobody else imports asyncio.
    """
    _instance: _EventLoopThread | None = None
    _lock = threading.Lock()
    _TEARDOWN_WINERRORS = frozenset({64, 1236, 10038, 10054})

    def __new__(cls) -> _EventLoopThread:
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                cls._instance = inst
                inst._start()
                atexit.register(inst.shutdown)
            return cls._instance

    def _start(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.loop.set_exception_handler(self._handle_loop_exception)
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()

        self._thread = threading.Thread(target=_run, name="connection-mgr-event-loop", daemon=True)
        self._thread.start()
        ready.wait()

    def _handle_loop_exception(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """
        Demote one Windows artifact to DEBUG: after an RST, asyncio's
        `_call_connection_lost` reports `sock.shutdown()` failing on a socket
        that is already dead. Everything else goes to the default handler.
        """
        exc = context.get("exception")
        if (isinstance(exc, OSError) and getattr(exc, "winerror", None) in self._TEARDOWN_WINERRORS
                and "_call_connection_lost" in str(context.get("handle", ""))):
            logger.debug("ignoring post-close socket teardown error: %s", exc)
            return
        loop.default_exception_handler(context)

    def await_coroutine(self, coro: Coroutine[Any, Any, Any], timeout: float | int | None = None) -> Any:
        """Run `coro` on the loop and block the caller until it finishes."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        # Two classes on 3.10, where asyncio.TimeoutError is not the builtin.
        except (asyncio.TimeoutError, concurrent.futures.TimeoutError) as exc:
            raise TimeoutError(str(exc)) from exc

    def call_on_loop(self, func: Callable[..., Any], *args: Any,
                     timeout: float | int | None = None) -> Any:
        """`await_coroutine` for a plain function."""
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def _run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(func(*args))
            except BaseException as exception:
                future.set_exception(exception)

        self.loop.call_soon_threadsafe(_run)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            raise TimeoutError(str(exc)) from exc

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        """Schedule `coro` without waiting for it."""
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

    Subclasses implement `_do_start`, `_do_stop` and `_do_send` (all async, on
    the loop thread), feed every parsed inbound message to `_dispatch_incoming`,
    and report peer state through `_mark_unit_connected` /
    `_mark_unit_disconnected` -- the single trigger for the echo lifecycle and
    for `wait_for_connected_units`. Override `_do_disconnect_unit` if one unit's
    transport can be closed on its own.

    Route ownership lives in `_routes.RouteTable`, heartbeats in
    `_echo.UnitEchoSupervisor`; both are private collaborators on the loop thread.
    """
    can_send: bool = True
    can_receive: bool = True

    def __init__(self, config: ConnectionConfig) -> None:
        self.config = config
        # Loaded here, not by the manager, so a connection built directly still
        # knows its links -- and a config naming units its structures don't
        # define fails before any socket opens.
        self._interfaces: dict[UnitName, Interface] = load_interfaces(config)
        self._loop_thread = get_event_loop_thread()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._started = False
        self._retry = False
        # True while tearing down: nothing new may be dispatched, armed or spawned.
        self._closing = False
        # Bumped by every close(), so a waiter parked across it is released.
        self._generation = 0
        self._unit_codes: dict[UnitName, UnitCode] = config.unit_codes
        self._own_unit_code: UnitCode = config.unitCode
        self._active_units: set[UnitName] = set()
        # Swapped, never cleared -- see `_notify_state_change`.
        self._state_event: asyncio.Event = asyncio.Event()
        self._routes: RouteTable = RouteTable()
        self._periodic_tasks: dict[PeriodicKey, asyncio.Task[None]] = {}
        self._echo: EchoSettings = config.echo
        self._unit_echo: dict[UnitName, EchoSettings] = config.unit_echoes
        self._echo_supervisor: UnitEchoSupervisor = UnitEchoSupervisor(self)
        atexit.register(self.close)

    # ------------------------------------------------------------------ #
    # Unit resolution
    # ------------------------------------------------------------------ #
    def _unit_code_for(self, unit_name: str) -> UnitCode:
        code = self._unit_codes.get(unit_name)
        if code is not None:
            return code
        raise ValueError(f"No unit code for unit {unit_name!r}; known units: {list(self._unit_codes)}")

    def _resolve_unit(self, unit_name: str | None) -> UnitName:
        units = self.config.unit_names
        if unit_name is not None:
            if unit_name in units:
                return unit_name
            raise ValueError(f"Unknown unit {unit_name!r}; known units: {units}")
        if len(units) == 1:
            return units[0]
        raise ValueError(f"unit_name is required: this connection has multiple units {units}")

    def _resolve_route(self, unit_name: str | None, key: MessageKey) -> tuple[UnitName, RouteKey]:
        """The caller's optional unit name -> (unit name, inbound route key)."""
        unit = self._resolve_unit(unit_name)
        return unit, (self._unit_code_for(unit), key)

    # ------------------------------------------------------------------ #
    # Message keys -- what a caller's selector routes under
    # ------------------------------------------------------------------ #
    def _message_key(self, opCode: OpCode | None) -> MessageKey:
        """
        The route key a caller's selector names. Framed links route on the opcode
        (an int, a "0x.." string, or the IRS message itself); DDS overrides this
        to route on the topic.
        """
        if opCode is None:
            raise TypeError(f"an opcode is required on {self.config.protocol.value} connections")
        return extract_opcode(opCode)

    def _send_key(self, data: Any, opCode: OpCode | None) -> MessageKey:
        """
        The key an outbound `data` goes out under. A message carries its own
        route from `@Definition`, so `opCode` is optional for it and, if given,
        must agree -- and its unit must be US, since the header is stamped with
        our code. Bytes carry nothing, so `opCode` is required (as it is with no
        data at all, for `stop_periodic`). DDS derives the key from the sample.
        """
        if data is None or isinstance(data, (bytes, bytearray, memoryview)):
            return self._message_key(opCode)
        if not isinstance(data, Message):
            raise TypeError(
                f"send_message takes a @Definition message instance or bytes, got {type(data).__name__}")
        message_class = type(data)
        if not is_definition(message_class):
            raise TypeError(f"{message_class.__name__} has no @Definition(unitCode=..., opCode=...)")
        if message_class._unitCode != self._own_unit_code:
            raise ValueError(
                f"{message_class.__name__} is sent by unit {message_class._unitCode}, but this "
                f"connection is unit {self._own_unit_code}")
        own = message_class._opCode
        if opCode is not None and self._message_key(opCode) != own:
            raise ValueError(
                f"the message is opcode {own}, but the call names opcode {self._message_key(opCode)}")
        return own

    def _send_unit(self, unit_name: str | None, key: MessageKey) -> UnitName | None:
        """The destination of a send. None only on DDS, where a sample goes to
        every subscriber of its topic."""
        return self._resolve_unit(unit_name)

    def _periodic_key(self, unit_name: UnitName | None, key: MessageKey) -> PeriodicKey:
        return (None if unit_name is None else self._unit_code_for(unit_name)), key

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, retry: bool = False) -> None:
        """
        Open every transport and block until inbound traffic can be accepted.

        `retry=True` keeps retrying a refused connection once a second, with no
        limit, and lets TCP clients reconnect after their peer drops.
        """
        if self._started:
            return
        self._retry = retry
        self._loop_thread.await_coroutine(self._startup_all(retry))
        self._started = True

    async def _startup_all(self, retry: bool) -> None:
        self._closing = False
        attempt = 0
        while True:
            existing = set(self._tasks)
            try:
                await self._do_start()
                return
            except BaseException as exc:
                await self._abort_start(existing)
                if not (retry and isinstance(exc, ConnectionRefusedError)):
                    raise
            attempt += 1
            logger.info("connection refused, retrying (attempt %d)", attempt)
            await asyncio.sleep(1)

    async def _abort_start(self, keep: set[asyncio.Task[Any]]) -> None:
        """Undo a partial `_do_start` -- close what it opened, cancel the tasks it
        spawned -- so neither a retry nor a later start() inherits half of it.
        Tasks in `keep` predate this attempt (e.g. periodic schedules) and stay."""
        self._closing = True
        try:
            await self._do_stop()
        except Exception:
            logger.exception("cleanup after a failed start raised")
        for unit in list(self._active_units):
            self._mark_unit_disconnected(unit)
        spawned = [task for task in self._tasks - keep if not task.done()]
        for task in spawned:
            task.cancel()
        await asyncio.gather(*spawned, return_exceptions=True)
        self._closing = False

    def close(self, timeout: float | int | None = 5.0) -> None:
        """
        Close every transport, cancel every background task and fail every parked
        `receive_message()` with `ConnectionError`. Standing on-receive/on-connect
        registrations are kept, so a connection restarted with `start()` keeps
        its handlers.
        """
        if not self._started:
            return
        self._loop_thread.await_coroutine(self._shutdown_all(), timeout=timeout)
        self._started = False

    async def _shutdown_all(self) -> None:
        self._closing = True
        await self._do_stop()
        self._periodic_tasks.clear()
        self._echo_supervisor.forget_all()
        self._active_units.clear()
        self._generation += 1
        self._notify_state_change()

        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._routes.fail_all_subscriptions(ConnectionError("connection closed"))

    def _track(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Schedule a background coroutine that `close()` will cancel and await."""
        task = self._loop_thread.loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ #
    # Per-unit connection state -- the trigger for the echo lifecycle
    # ------------------------------------------------------------------ #
    @property
    def active_units(self) -> set[str]:
        """Snapshot of the units that currently have a live peer."""
        return set(self._active_units)

    def _notify_state_change(self) -> None:
        """Wake every `wait_for_connected_units` waiter. Swapped rather than
        cleared, so a waiter not yet scheduled still sees its own event fire."""
        self._state_event.set()
        self._state_event = asyncio.Event()

    def _mark_unit_connected(self, unit_name: str) -> None:
        """Called by a subclass once `unit_name` has a usable peer. Idempotent:
        UDP calls it on every datagram."""
        if self._closing or unit_name in self._active_units:
            return
        self._active_units.add(unit_name)
        logger.info("Unit %s connected", unit_name)
        self._echo_supervisor.arm(unit_name)
        callback = self._routes.connect_callback(unit_name)
        if callback is not None:
            self._track(self._run_connect_callback(callback, unit_name))
        self._notify_state_change()

    def _mark_unit_disconnected(self, unit_name: str) -> None:
        """Called by a subclass once `unit_name` lost its peer. Idempotent."""
        self._echo_supervisor.disarm(unit_name)
        if unit_name not in self._active_units:
            return
        self._active_units.discard(unit_name)
        logger.info("Unit %s disconnected", unit_name)
        self._notify_state_change()

    # ------------------------------------------------------------------ #
    # Per-unit settings lookups (both resolved once, at config load)
    # ------------------------------------------------------------------ #
    def _echo_for(self, unit_name: UnitName) -> EchoSettings:
        return self._unit_echo.get(unit_name, self._echo)

    def interface_for(self, unit_name: str | None = None) -> Interface:
        """The IRS interface of the link to `unit_name` (omittable with one unit)."""
        unit = self._resolve_unit(unit_name)
        interface = self._interfaces.get(unit)
        if interface is None:
            raise ValueError(f"{self.config.protocol.value} connections have no IRS interface")
        return interface

    async def _disconnect_unit(self, unit_name: str, reason: str = "echo timeout") -> None:
        """
        Retire one unit without disturbing the others: cancel its periodic
        senders, fail its parked receives, drop its on-receive callbacks, and
        close its transport.
        """
        unit_code = self._unit_codes.get(unit_name)

        for route_key in [key for key in self._periodic_tasks if key[0] == unit_code]:
            task = self._periodic_tasks.pop(route_key)
            if not task.done():
                task.cancel()

        self._routes.drop_unit(unit_code, ConnectionError(f"unit {unit_name!r} disconnected: {reason}"))
        self._mark_unit_disconnected(unit_name)
        try:
            await self._do_disconnect_unit(unit_name)
        except Exception:
            logger.exception("error disconnecting unit %s", unit_name)

    # ------------------------------------------------------------------ #
    # Incoming message dispatch
    # ------------------------------------------------------------------ #
    def _dispatch_incoming(self, unit_name: str, key: MessageKey, payload: Any) -> None:
        """
        Route one inbound message (loop thread). Echoes are consumed; unowned
        routes are dropped before decoding; a message IRS can't parse is logged
        and dropped without releasing the route's owner -- a bad frame must not
        fail a caller waiting for a good one.
        """
        if self._closing or self._echo_supervisor.consume(unit_name, key):
            return
        route_key: RouteKey = (self._unit_code_for(unit_name), key)
        owned = self._routes.owner_of(route_key)
        if owned is None:
            return
        owner, message_class = owned
        try:
            message = self._decode(message_class, payload)
        except Exception as exc:
            logger.exception(
                "dropping a message IRS could not parse (unit=%s, %s): %s",
                unit_name, describe_key(key), exc)
            return
        self._deliver(owner, route_key, message, unit_name, key)

    def _deliver(self, owner: Owner, route_key: RouteKey, message: IrsMessage,
                 unit_name: str, key: MessageKey) -> None:
        """Release a parked `receive_message()` (freeing its route), or run the
        standing callback on an executor thread (keeping it)."""
        if isinstance(owner, asyncio.Future):
            self._routes.settle(route_key, owner)
            owner.set_result(message)
            return
        self._track(self._run_callback(owner, message, unit_name, key))

    # ------------------------------------------------------------------ #
    # Codec hooks -- native payloads by default; FramedConnection adds IRS
    # ------------------------------------------------------------------ #
    @abstractmethod
    def _route_class(self, unit_name: UnitName, selector: OpCode, route_key: RouteKey) -> MessageClass:
        """
        The class `route_key`'s payloads are parsed with, raising if this
        connection could never deliver it -- subscribing to a message we don't
        define is our bug, and must not become a `receive_message` that never
        returns.
        """
        ...

    def _claim_route(self, unit_name: str | None, selector: OpCode) -> tuple[RouteKey, MessageClass]:
        """A caller's (unit, selector) -> the route to own and its parse class."""
        unit, route_key = self._resolve_route(unit_name, self._message_key(selector))
        return route_key, self._route_class(unit, selector, route_key)

    def _encode(self, key: MessageKey, message: Any, unit_name: UnitName | None) -> Any:
        """Application message -> wire payload."""
        return message

    def _decode(self, message_class: MessageClass, payload: Any) -> Any:
        """Wire payload -> application message."""
        return payload

    async def _run_callback(
        self, callback: ReceiveCallback, payload: IrsMessage, unit_name: str, key: MessageKey
    ) -> None:
        """
        Run one on-receive callback on an executor thread. Inline on the loop it
        would stall every connection, and any sync API call it made would
        deadlock waiting on the loop it is blocking.
        """
        try:
            await self._loop_thread.loop.run_in_executor(None, callback, payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad callback must not kill the read loop
            logger.exception(
                "on-receive callback for unit %s (%s) raised", unit_name, describe_key(key)
            )

    async def _run_connect_callback(self, callback: ConnectCallback, unit_name: str) -> None:
        """Run one on-connect callback on an executor thread, for the same
        reason as `_run_callback`."""
        try:
            await self._loop_thread.loop.run_in_executor(None, callback, unit_name)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad callback must not kill the caller's read loop
            logger.exception("on-connect callback for unit %s raised", unit_name)

    # ------------------------------------------------------------------ #
    # Waiting on a claimed route (runs on the loop thread)
    # ------------------------------------------------------------------ #
    async def _await_subscription(
        self, route_key: RouteKey, future: asyncio.Future[IrsMessage], timeout: float | int | None
    ) -> IrsMessage:
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            # Free the route however this ended, so an abandoned one never
            # swallows a later message.
            self._routes.settle(route_key, future)

    # ------------------------------------------------------------------ #
    # Public sync API
    # ------------------------------------------------------------------ #
    def send_message(self, data: Outbound, opcode: OpCode | None = None,
                     unit_name: str | None = None) -> None:
        """
        Send `data` to `unit_name`, which may be omitted when only one unit is
        configured. A `@Definition` message goes out under its own opcode;
        bytes pass through and need `opcode`. On DDS the sample names its topic.
        """
        key = self._send_key(data, opcode)
        unit = self._send_unit(unit_name, key)
        payload = self._encode(key, data, unit)
        self._loop_thread.await_coroutine(self._do_send(unit, payload, key))

    def wait_for_connected_units(
        self, target: ConnectedTarget, timeout: float | int | None = None
    ) -> bool:
        """
        Block until `target` is connected: an `int` (at least that many units),
        a unit name, or an iterable of names (all of them). Returns False on
        timeout, or if the connection is closed while waiting.
        """
        predicate = self._build_unit_predicate(target)
        wait_timeout = timeout + 1 if timeout is not None else None
        met: bool = self._loop_thread.await_coroutine(
            self._wait_for_units(predicate, timeout), timeout=wait_timeout
        )
        return met

    def _build_unit_predicate(self, target: ConnectedTarget) -> Callable[[], bool]:
        """Validate `target` against the configured units in the caller's thread,
        so an impossible wait fails now instead of never returning."""
        configured = set(self.config.connections)
        if isinstance(target, str):
            if target not in configured:
                raise ValueError(
                    f"Unknown unit {target!r}; known units: {sorted(configured)}"
                )
            return lambda: target in self._active_units
        if isinstance(target, bool):
            raise TypeError(f"target must be an int, str or list[str], got {target!r}")
        if isinstance(target, int):
            if not 0 <= target <= len(configured):
                raise ValueError(
                    f"cannot wait for {target} connected units: this connection has "
                    f"{len(configured)} configured ({sorted(configured)})"
                )
            return lambda: len(self._active_units) >= target
        names = set(target)
        unknown = names - configured
        if unknown:
            raise ValueError(
                f"Unknown unit(s) {sorted(unknown)}; known units: {sorted(configured)}"
            )
        return lambda: names <= self._active_units

    async def _wait_for_units(self, predicate: Callable[[], bool], timeout: float | int | None) -> bool:
        generation = self._generation

        async def _wait() -> bool:
            # Re-read `_state_event` every pass: it is swapped on each change.
            while not predicate():
                if self._generation != generation:
                    return False  # closed underneath us
                await self._state_event.wait()
            return True

        if timeout is None:
            return await _wait()
        try:
            return await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            return False

    def receive_message(self, opcode: OpCode, unit_name: str | None = None,
        timeout: float | int | None = None, trigger_function: TriggerFunction | None = None) -> IrsMessage:
        """
        Block until a message on (unit, opcode) arrives and return it, parsed
        with the route's class. `opcode` is the message class itself or its
        int/"0x.." opcode, resolved against that unit's structures.

        Nothing is buffered: only an in-flight call receives, and only one may be
        in flight per route. `trigger_function` runs after the route is claimed,
        so a reply it solicits cannot be lost; if it raises, the route is released
        and the exception propagates. On DDS, `opcode` is the topic.

        Raises `TimeoutError` on timeout and `ConnectionError` if the unit drops
        or the connection closes while waiting.
        """
        route_key, message_class = self._claim_route(unit_name, opcode)
        future: asyncio.Future[IrsMessage] = self._loop_thread.call_on_loop(
            self._routes.claim, route_key, message_class, self._loop_thread.loop
        )
        if trigger_function is not None:
            try:
                trigger_function()
            except BaseException:
                self._loop_thread.call_on_loop(self._routes.release, route_key, future)
                raise
        wait_timeout = timeout + 1 if timeout is not None else None
        message: IrsMessage = self._loop_thread.await_coroutine(
            self._await_subscription(route_key, future, timeout), timeout=wait_timeout
        )
        return message

    def handle_on_receive(self, opcode: OpCode,
        callback_func: ReceiveCallback, unit_name: str | None = None) -> None:
        """
        Call `callback_func(message)` for every message on (unit, opcode) until
        `stop_on_receive()`.

        Runs on an executor thread, so it may block and call this connection's
        sync API; it may also run concurrently with itself. Exceptions are logged.
        A route is either polled or handled -- registering over a parked
        `receive_message` or another callback raises `RuntimeError`.
        """
        if not callable(callback_func):
            raise TypeError(f"callback_func must be callable, got {callback_func!r}")
        route_key, message_class = self._claim_route(unit_name, opcode)
        self._loop_thread.call_on_loop(
            self._routes.register_callback, route_key, callback_func, message_class)

    def stop_on_receive(self, opcode: OpCode, unit_name: str | None = None) -> bool:
        """Remove the route's standing callback. True if there was one; a call
        already running is left to finish."""
        key = self._message_key(opcode)
        _unit, route_key = self._resolve_route(unit_name, key)
        removed: bool = self._loop_thread.call_on_loop(self._routes.unregister_callback, route_key)
        return removed

    def handle_on_connect(
        self,
        callback_func: ConnectCallback,
        unit_name: str | None = None,
    ) -> None:
        """
        Call `callback_func(unit_name)` each time the unit gains a peer, on an
        executor thread (so it may `send_message` a greeting). Not retroactive:
        a unit already connected fires on its next connect. One callback per
        unit; `stop_on_connect()` first to replace it.
        """
        if not callable(callback_func):
            raise TypeError(f"callback_func must be callable, got {callback_func!r}")
        unit = self._resolve_unit(unit_name)
        self._loop_thread.call_on_loop(self._routes.register_connect, unit, callback_func)

    def stop_on_connect(self, unit_name: str | None = None) -> bool:
        """Remove the unit's on-connect callback. True if there was one."""
        unit = self._resolve_unit(unit_name)
        removed: bool = self._loop_thread.call_on_loop(self._routes.unregister_connect, unit)
        return removed

    def periodic_sending(
        self,
        data: Outbound,
        interval: int | float,
        opcode: OpCode | None = None,
        unit_name: str | None = None,
    ) -> None:
        """
        `send_message` every `interval` seconds until `stop_periodic`. Calling it
        again for the same route replaces the schedule. `data` is encoded once,
        here, so an unencodable message fails now rather than every tick.
        """
        interval_seconds = float(interval)
        if interval_seconds <= 0:
            raise ValueError(f"interval must be > 0 seconds, got {interval!r}")
        key = self._send_key(data, opcode)
        unit = self._send_unit(unit_name, key)
        payload = self._encode(key, data, unit)
        self._loop_thread.await_coroutine(
            self._start_periodic(unit, self._periodic_key(unit, key), payload, key, interval_seconds))

    def stop_periodic(self, opcode: OpCode, unit_name: str | None = None) -> bool:
        """Stop the schedule `periodic_sending` started for this route. True if
        one was running."""
        key = self._send_key(None, opcode)
        unit = self._send_unit(unit_name, key)
        stopped: bool = self._loop_thread.await_coroutine(self._stop_periodic(self._periodic_key(unit, key)))
        return stopped

    # -- periodic sending internals (all run on the loop thread) ---------- #
    async def _start_periodic(self, unit_name: UnitName | None, periodic_key: PeriodicKey, data: Any,
                              key: MessageKey, interval: float) -> None:
        await self._stop_periodic(periodic_key)
        task = self._track(self._periodic_send_loop(unit_name, data, key, interval))
        self._periodic_tasks[periodic_key] = task

        def _forget(finished: asyncio.Task[Any], stored: PeriodicKey = periodic_key) -> None:
            if self._periodic_tasks.get(stored) is finished:
                del self._periodic_tasks[stored]

        task.add_done_callback(_forget)

    async def _stop_periodic(self, periodic_key: PeriodicKey) -> bool:
        task = self._periodic_tasks.pop(periodic_key, None)
        if task is None:
            return False
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return True

    async def _periodic_send_loop(
        self, unit_name: UnitName | None, data: Any, key: MessageKey, interval: float
    ) -> None:
        while True:
            try:
                await self._do_send(unit_name, data, key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "periodic send (unit=%s, %s) failed: %s", unit_name, describe_key(key), exc
                )
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ #
    # Subclass hooks
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def _do_start(self) -> None:
        """Open every transport. Return only once inbound traffic can be accepted.
        On failure, whatever was opened is closed through `_do_stop`."""
        ...

    @abstractmethod
    async def _do_stop(self) -> None:
        """Close everything `_do_start()` opened. Runs before task cancellation,
        so no read loop is woken by traffic mid-teardown. Must tolerate a
        partial start."""
        ...

    @abstractmethod
    async def _do_send(self, unit_name: UnitName | None, data: Any, opcode: MessageKey) -> None:
        """Transmit one message. `data` is wire bytes on framed links and a typed
        sample on DDS, where `unit_name` may be None (broadcast). Raise
        `ConnectionError` when the unit has no usable peer: periodic senders log
        and retry, the echo sender retires the unit."""
        ...

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """Close just this unit's transport (echo watchdog). The default keeps it
        open: a protocol that cannot isolate one unit must not cut the others."""
        logger.warning(
            "%s does not implement per-unit disconnect; unit %s left open",
            type(self).__name__, unit_name,
        )


class FramedConnection(Connection):
    """
    Base for the connections that frame payloads with the (UnitCode, OpCode,
    DataLength) header and carry IRS message bodies: TCP, UDP and Multicast.
    """

    def _frame(self, data: bytes, opcode: int) -> bytes:
        """Header (stamped with OUR unit code) + payload."""
        return pack_message(self._own_unit_code, opcode, data)

    def _route_class(self, unit_name: UnitName, selector: OpCode, route_key: RouteKey) -> IrsMessage:
        """The layout THEIR unit code sends on this opcode, from this link's own
        structures. A class selector must be exactly that layout."""
        unit_code, opcode = route_key
        if is_definition(selector) and selector._unitCode != unit_code:
            raise ValueError(
                f"{selector.__name__} is sent by unit {selector._unitCode}, not by "
                f"{unit_name!r} (unit {unit_code})")
        message_class = self._interfaces[unit_name].message_class(unit_code, opcode)
        if is_definition(selector) and selector is not message_class:
            same_name = selector.__qualname__ == message_class.__qualname__
            raise ValueError(
                f"{selector.__module__}.{selector.__qualname__} is not part of the link to "
                f"{unit_name!r}: its structures define {message_class.__module__}."
                f"{message_class.__qualname__} on this route"
                + (" -- the same file imported twice under two names; import the message the "
                   "way the config names the file, or take it from interface_for()" if same_name else ""))
        return message_class

    def _encode(self, opcode: OpCode, message: Message | bytes, unit_name: UnitName) -> bytes:
        """The message's own layout; bytes pass through. Stamped with OUR unit
        code by `_frame`, which `_send_key` already checked the message is."""
        if isinstance(message, (bytes, bytearray, memoryview)):
            return message
        try:
            return message.to_bytes()
        except Exception as exc:
            raise IRSDataError(f"{type(message).__name__}.to_bytes() failed: {exc}") from exc

    def _decode(self, message_class: IrsMessage, payload: bytes) -> Message:
        """Parsed with the class the route was claimed with."""
        try:
            return parse(message_class, payload)
        except Exception as exc:
            raise IRSDataError(f"{message_class.__name__}.from_bytes() failed: {exc}") from exc
