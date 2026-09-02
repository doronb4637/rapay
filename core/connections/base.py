"""
Core abstractions shared by every protocol implementation:

  * `_EventLoopThread` -- the single sync <-> async bridge for the whole
    process (see its docstring for the rationale).
  * `Connection` -- the ABC every protocol (TCP/UDP/Multicast/DDS) implements.
    It owns unit-name/unit-code resolution, per-unit connection state, the
    echo lifecycle (periodic sending + liveness watchdog), subscribe-or-drop
    message filtering, on-receive callbacks, periodic application sends, task
    tracking for absolute teardown
"""
from __future__ import annotations
import asyncio
import atexit
import concurrent.futures
import logging
import threading
import time
from abc import ABC, abstractmethod
from asyncio import Event
from typing import Any, Callable, Coroutine, Iterable, Protocol

from core.annotations import IrsMessage, Namespace, OpCode, UnitCode
from core.IRS.irs_parser import IRSDataError, irs_to_bytes, parse_irs, validate_irs
from core.tools.general import extract_opcode

from ._echo import UnitEchoSupervisor
from ._routes import ConnectCallback, ReceiveCallback, RouteKey, RouteTable, UnitName
from .config import ConnectionConfig, EchoSettings
from .framing import pack_message

logger = logging.getLogger("connmgr")

""" Annotations """
TriggerFunction = Callable[[], Any]
ConnectedTarget = UnitCode | UnitName | Iterable[UnitName]


class Unit(Protocol):
    """
    What a logical UNIT offers, whether it happens to be a single `Connection`
    or a `CompositeUnit` assembled from several direction-limited ones.

    `ConnectionManager.create` / `create_composite` hand's it back,
    """

    @property
    def active_units(self) -> set[str]: ...
    def start(self, retry: bool = False) -> None: ...
    def close(self, timeout: float | int | None = 5.0) -> None: ...
    def send_message(self, data: IrsMessage | dict, opcode: int | None = None, unit_name: str | None = None) -> None: ...
    def receive_message(self, opcode: int | str | IrsMessage, unit_name: str | None = None,
                        timeout: float | int | None = None, trigger_function: TriggerFunction | None = None) -> IrsMessage: ...
    def handle_on_receive(self, opcode: int | str | IrsMessage, callback_func: ReceiveCallback, unit_name: str | None = None) -> None: ...
    def stop_on_receive(self, opcode: int | str | IrsMessage, unit_name: str | None = None) -> bool: ...
    def handle_on_connect(self, callback_func: ConnectCallback, unit_name: str | None = None) -> None: ...
    def stop_on_connect(self, unit_name: str | None = None) -> bool: ...
    def periodic_sending(self, data: IrsMessage | dict[str, Any], opcode: int | None,
                         interval: int | float, unit_name: str | None = None) -> None: ...
    def stop_periodic(self, opcode: int | str | IrsMessage,
                      unit_name: str | None = None) -> bool: ...
    def wait_for_connected_units(self, target: ConnectedTarget,
                                 timeout: float | int | None = None) -> bool: ...


class _EventLoopThread:
    """
    Owns exactly ONE background thread running an asyncio event loop for the
    entire process -- this is the sync <-> async bridge:

      - All actual socket I/O (asyncio streams / transports / protocols)
        runs as coroutines *inside* this loop: cheap, single-threaded,
        non-blocking, and scales to many connections without needing one OS
        thread per connection
      - This let the caller use synchronous API for `Connection.start/.stop/.send_message`,
        etc. Each call is marshaled onto the loop thread

    This object is a singleton: one loop/thread services that handles
    every Connection instance.
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
        Demote one specific piece of Windows teardown noise, and nothing else.

        when `_call_connection_lost` calls `sock.shutdown()` on the specific socket.
        When the peer sent an RST, that shutdown raises WSAENOTSOCK/ERROR_NETNAME_DELETED and
        asyncio reports it as an unhandled error -- after the transport is
        already dead, so there is nothing to act on and nothing is lost.

        The match is deliberately narrow everything else, including any other
        OSError, goes to the default handler untouched.
        """
        exc = context.get("exception")
        if (isinstance(exc, OSError) and getattr(exc, "winerror", None) in self._TEARDOWN_WINERRORS
                and "_call_connection_lost" in str(context.get("handle", ""))):
            logger.debug("ignoring post-close socket teardown error: %s", exc)
            return
        loop.default_exception_handler(context)

    def await_coroutine(self, coro: Coroutine[Any, Any, Any], timeout: float | int | None = None) -> Any:
        """Submit a coroutine and blocks the CALLER until completion
        (Simulate normal sync)."""
        # TODO change to this current code when dropping python 3.10
        #future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        #return future.result(timeout=timeout)
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        except (asyncio.TimeoutError, concurrent.futures.TimeoutError) as exc:
            raise TimeoutError(str(exc)) from exc

    def call_on_loop(self, func: Callable[..., Any], *args: Any,
                     timeout: float | int | None = None) -> Any:
        """Acts the same as 'await_coroutine' just for synchronous functions."""
        future = concurrent.futures.Future()

        def _run() -> None:
            # changes future state to 'RUNNING'
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
        """Non-blocking submission for fire-and-forget call."""
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

    Subclasses must also report per-unit peer state by calling
    `_mark_unit_connected()` / `_mark_unit_disconnected()` (both on the loop
    thread) the moment a unit gains or loses a usable peer. That state is the
    single trigger for the echo lifecycle -- echoes to a unit start when it
    connects, stop when it drops, and start again if it comes back -- and is
    what `wait_for_connected_units()` blocks on.

    Everything protocol-independent lives at this level so subclasses stay
    small: unit resolution, the sync/async marshaling, periodic sending, and
    absolute-teardown task bookkeeping here in this class; route ownership and
    subscribe-or-drop filtering in `_routes.RouteTable`; the heartbeat, liveness
    clock and timeout watchdog in `_echo.UnitEchoSupervisor`. Both collaborators
    are owned by this connection and run on its loop thread -- they are not a
    layer a caller ever sees.
    """
    can_send: bool = True
    can_receive: bool = True
    #: DDS doesn't use irs parsing.
    uses_irs_parser: bool = False

    def __init__(self, config: ConnectionConfig) -> None:
        self.config = config
        self._loop_thread = get_event_loop_thread()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._started = False
        self._unit_codes: dict[UnitName, UnitCode] = config.unit_codes
        self._own_unit_code: UnitCode = config.unitCode
        self._active_units: set[UnitName] = set()
        # triggered when a new connection is established.
        self._state_event: asyncio.Event = asyncio.Event()
        # Who owns each (unit_code, opcode) route, and the on-connect callbacks
        self._routes: RouteTable = RouteTable()
        self._periodic_tasks: dict[RouteKey, asyncio.Task[None]] = {}
        # Resolved PER UNIT at config load (see config.EchoSettings.resolve):
        self._echo: EchoSettings = config.echo
        self._unit_echo: dict[UnitName, EchoSettings] = config.unit_echoes
        # Heartbeat senders, liveness clocks and timeout watchdogs, one set per
        # unit. See _echo.UnitEchoSupervisor.
        self._echo_supervisor: UnitEchoSupervisor = UnitEchoSupervisor(self)
        # Likewise per unit (see config.resolve_structures): which IRS
        # structures modules scope this link's layouts.
        self._structures: tuple[Namespace, ...] = config.structures
        self._unit_structures: dict[UnitName, tuple[Namespace, ...]] = config.unit_structures
        # Registered once per connection, not once per start(): `close()` is
        # idempotent and safe on a connection that never started, so a single
        # hook covers every start/close cycle this object goes through. Doing it
        # in start() piled up a duplicate handler on every restart.
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
        units = self.config.connected_units
        if unit_name is not None:
            if unit_name in units:
                return unit_name
            raise ValueError(f"Unknown unit {unit_name!r}; known units: {units}")
        if len(units) == 1:
            return units[0]
        raise ValueError(f"unit_name is required: this connection has multiple connected units {units}")

    def _resolve_route(self, unit_name: str | None, opcode: OpCode) -> tuple[UnitName, RouteKey]:
        """Resolve the caller's optional unit name into both the name (for
        the protocol layer and for returning to the caller) and the
        Route-Key every internal table is keyed by."""
        unit = self._resolve_unit(unit_name)
        return unit, (self._unit_code_for(unit), opcode)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, retry: bool = False) -> None:
        """
        Boot this connection's in the background loop,
        blocks until it is actually listening / connected.
        """
        if self._started:
            return
        self._loop_thread.await_coroutine(self._startup_all(retry))
        self._started = True

    async def _startup_all(self, retry: bool) -> None:
        index = 0
        while True:
            try:
                await self._do_start()
                return
            except ConnectionRefusedError as exc:
                if not retry or getattr(exc, 'winerror', None) != 1225:
                    raise exc
                index += 1
                logger.info(f"Server refused connection trying to reconnect, time: {index}")
                await asyncio.sleep(1)

    def close(self, timeout: float | int | None = 5.0) -> None:
        """
        Sync entrypoint for an ABSOLUTE teardown:

          1. `_do_stop()` closes every socket/transport/server this
             connection owns, so no new inbound connection or datagram can
             ever be accepted again.
          2. Every background task this connection ever spawned via
             `_track()` (read loops, echo senders, echo watchdogs, periodic
             senders, in-flight on-receive callbacks) is explicitly canceled
             and *awaited*, so nothing is left running on the shared loop
             thread after this call returns.
          3. Any receive_message() call still parked waiting on a
             subscription is released (its future is canceled) instead of
             being left to hang forever, and every standing callback is
             dropped.

        Idempotent, and safe to call on a connection that was never started.
        """
        if not self._started:
            return
        self._loop_thread.await_coroutine(self._shutdown_all(), timeout=timeout)
        self._started = False

    async def _shutdown_all(self) -> None:
        await self._do_stop()
        self._periodic_tasks.clear()
        self._echo_supervisor.forget_all()
        # Registrations go BEFORE the task sweep, so nothing dispatched on the
        # way down can still find a callback to invoke.
        self._routes.drop_all_callbacks()
        self._active_units.clear()
        self._notify_state_change()  # release anyone parked in wait_for_connected_units

        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

        # Parked receives go LAST, so a caller is woken to a connection that is
        # fully down rather than one mid-collapse.
        self._routes.cancel_all_subscriptions()

    def _track(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Subclass helper: schedule a background coroutine and register it
        so `close()` can guarantee it's canceled and awaited.
        ! No orphaned tasks - ever !"""
        task = self._loop_thread.loop.create_task(coro)
        self._tasks.add(task)
        # add_done_callback() call and pass 'task' variable to discard.
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ #
    # Per-unit connection state -- the trigger for the echo lifecycle
    # ------------------------------------------------------------------ #
    @property
    def active_units(self) -> set[str]:
        """Returns all connected units"""
        return set(self._active_units)

    def _notify_state_change(self) -> None:
        """Wake every `wait_for_connected_units` waiter. The event is swapped
        rather than cleared so a waiter that hasn't been scheduled yet still
        sees its own event fire."""
        self._state_event.set()
        self._state_event = asyncio.Event()

    def _mark_unit_connected(self, unit_name: str) -> None:
        """Called by a subclass once `unit_name` has a usable peer.
        Then start the echo cycle"""
        # For UDP connections
        if unit_name in self._active_units:
            return
        self._active_units.add(unit_name)
        logger.info(f"Unit {unit_name} connected")
        self._echo_supervisor.arm(unit_name)
        callback = self._routes.connect_callback(unit_name)
        if callback is not None:
            self._track(self._run_connect_callback(callback, unit_name))
        self._notify_state_change()

    def _mark_unit_disconnected(self, unit_name: str) -> None:
        """Called by a subclass once `unit_name` disconnected,
         Stops that unit's echo."""
        if unit_name not in self._active_units:
            self._echo_supervisor.disarm(unit_name)
            return
        self._active_units.discard(unit_name)
        logger.info(f"Unit {unit_name} disconnected")
        self._echo_supervisor.disarm(unit_name)
        self._notify_state_change()

    # ------------------------------------------------------------------ #
    # Per-unit settings lookups (both resolved once, at config load)
    # ------------------------------------------------------------------ #
    def _echo_for(self, unit_name: UnitName) -> EchoSettings:
        """The echo settings governing ONE unit -- its own if the config gave
        it any, this connection's block otherwise. Already merged and
        validated at load time (`config.EchoSettings.resolve`), so this is a
        dict lookup on the connect and dispatch paths, not a re-parse."""
        return self._unit_echo.get(unit_name, self._echo)

    def _structures_for(self, unit_name: UnitName) -> tuple[Namespace, ...]:
        """The IRS structures namespaces scoping ONE unit's layouts -- its own
        if the config gave it any, this connection's block otherwise. Resolved
        at load time (`config.resolve_structures`), so this is a dict lookup on
        the send and dispatch paths.

        An empty result means UNSCOPED: every registered module is searched.
        That is what a byte-oriented unit gets, and what every config written
        before structures were per-link keeps getting -- the parser only
        complains if two modules genuinely disagree about a route.
        """
        return self._unit_structures.get(unit_name) or self._structures

    async def _disconnect_unit(self, unit_name: str) -> None:
        """
        Tear down one unit's transport without disturbing the others, and
        make sure nothing is left waiting on a link that is now dead.
        """
        unit_code = self._unit_codes.get(unit_name)

        for route_key in [key for key in self._periodic_tasks if key[0] == unit_code]:
            task = self._periodic_tasks.pop(route_key)
            if not task.done():
                task.cancel()

        self._routes.drop_unit(
            unit_code, ConnectionError(f"unit {unit_name!r} disconnected: echo timeout")
        )

        # stops echo and releases unit-state waiters.
        self._mark_unit_disconnected(unit_name)
        # 4. close on protocol-level.
        try:
            await self._do_disconnect_unit(unit_name)
        except Exception:
            logger.exception("error disconnecting unit %s", unit_name)

    # ------------------------------------------------------------------ #
    # Incoming message dispatch
    # ------------------------------------------------------------------ #
    def _dispatch_incoming(self, unit_name: str, opcode: int, payload: bytes) -> None:
        """
        Called by a subclass's read loop / datagram callback (always on the
        shared event-loop thread) whenever a complete inbound message has been
        parsed. This is the single choke point, and it is three questions in a
        row:

          1. Is this an echo? `UnitEchoSupervisor.consume` answers per unit and,
             if so, has already refreshed that unit's liveness. The message goes
             no further and is never visible to the application, no matter what
             is or is not subscribed.
          2. Does anyone own this route? `RouteTable.owner_of` returns the
             parked `receive_message()` future, else the standing
             `handle_on_receive()` callback, else None -- in which case the
             message is discarded immediately, never buffered for a
             hypothetical future call.
          3. Does it decode? Only now, with a route owner known, is the payload
             handed to `IRS.irs_parser`, so unowned messages cost nothing. A
             message that does not decode costs exactly itself: it is logged and
             dropped, the route keeps its owner, and the read loop carries on.
             A parked `receive_message()` therefore stays parked and goes on
             waiting for a message it can actually return, up to its own
             `timeout` -- one peer sending a malformed frame must not be able to
             fail a caller who asked for a good one.
        """
        if self._echo_supervisor.consume(unit_name, opcode):
            return
        unit_code = self._unit_code_for(unit_name)
        route_key: RouteKey = (unit_code, opcode)
        owner = self._routes.owner_of(route_key)
        if owner is None:
            return
        try:
            message = self._decode(unit_code, opcode, payload, unit_name)
        except Exception as exc:
            # logger.exception, not warning: an unparseable message is a real
            # problem worth a traceback, it just isn't this caller's problem.
            logger.exception(
                "dropping a message IRS could not parse (unit=%s, opcode=%s): %s",
                unit_name, opcode, exc)
            return
        if message is None:
            # `parse_irs` handing back a (name, None) pair. Nothing to deliver,
            # and delivering None would look like a message that said nothing.
            logger.warning(
                "dropping a message that decoded to nothing (unit=%s, opcode=%s)", unit_name, opcode)
            return
        self._deliver(owner, route_key, message, unit_name, opcode)

    def _deliver(self, owner: asyncio.Future[IrsMessage] | ReceiveCallback,
                 route_key: RouteKey, message: IrsMessage,
                 unit_name: str, opcode: int) -> None:
        """Hand one decoded message to the route's owner.

        A parked `receive_message()` is released here and its route freed; a
        standing callback is dispatched to an executor thread and the route
        stays registered for the next message.
        """
        if isinstance(owner, asyncio.Future):
            self._routes.settle(route_key, owner)
            owner.set_result(message)
            return
        self._track(self._run_callback(owner, message, unit_name, opcode))

    # ------------------------------------------------------------------ #
    # IRS codec boundary
    # ------------------------------------------------------------------ #
    def _validate_route(self, unit_name: UnitName, route_key: RouteKey) -> None:
        """
        Raise if this connection could never deliver `route_key`.

        Subscribing to a message our own side doesn't define is OUR bug, so it
        is caught here -- at the subscribing call -- rather than becoming a
        `receive_message` that quietly never returns. IRS-backed connections
        answer that question by asking IRS. Connections whose payloads are
        native (DDS) have their own notion of a route that exists and override
        this; what they must not do is skip the check, which is how the
        question stops being asked at all.
        """
        validate_irs(*route_key, self._structures_for(unit_name))

    def _encode(self, opcode: int, message: IrsMessage, unit_name: UnitName) -> bytes | IrsMessage:
        """
        Application message -> wire payload, stamped with OUR unit code: the
        receiver needs to know who sent this, not who it was sent to.

        `unit_name` is REQUIRED even though it never reaches the wire, because
        it is what selects the layout. Our own unit code is identical for every
        peer on this connection, so it cannot distinguish two links that both
        define this opcode -- the destination's structures are the only thing
        that can, which is why this is not an optional convenience parameter.

        A no-op on connections whose payloads are native (DDS). Bytes are
        already wire form and pass straight through -- that is also how the
        config-supplied echo payload travels.
        """
        if not self.uses_irs_parser or isinstance(message, (bytes, bytearray, memoryview)):
            return message
        structures = self._structures_for(unit_name)
        try:
            return irs_to_bytes(self._own_unit_code, opcode, message, structures)
        except Exception as exc:
            raise IRSDataError(
                f"irs_to_bytes(unitCode={self._own_unit_code}, opCode={opcode}, "
                f"structures={list(structures) or 'any'}) failed: {exc}"
            ) from exc

    def _decode(self, unit_code: int, opcode: int, payload: bytes, unit_name: UnitName) -> IrsMessage:
        """
        Wire payload -> application message, parsed with THEIR unit code: the
        sender's identity is what selects the message layout, narrowed to the
        structures modules this particular link declared.
        """
        if not self.uses_irs_parser:
            return payload
        structures = self._structures_for(unit_name)
        try:
            parsed = parse_irs(unit_code, opcode, payload, structures)
        except Exception as exc:
            raise IRSDataError(
                f"parse_irs(unitCode={unit_code}, opCode={opcode}, "
                f"structures={list(structures) or 'any'}) failed: {exc}"
            ) from exc
        if parsed is None:
            return payload  # template parser: nothing to convert
        # parse_irs returns (message_name, message_object); the object is what
        # the caller asked for.
        return parsed[1] if isinstance(parsed, tuple) and len(parsed) == 2 else parsed

    async def _run_callback(
        self, callback: ReceiveCallback, payload: IrsMessage, unit_name: str, opcode: int
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

    async def _run_connect_callback(self, callback: ConnectCallback, unit_name: str) -> None:
        """Run one on-connect callback off the event-loop thread. Same
        reasoning as `_run_callback`: the callback is ordinary synchronous
        user code that may call back into this connection's sync API, and
        `_mark_unit_connected` -- which schedules this -- always runs ON the
        loop thread, so running the callback inline there would deadlock the
        first `send_message` inside it."""
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
            # Always free the route -- delivered, timed out or cancelled -- so
            # an abandoned one never swallows a later message.
            self._routes.settle(route_key, future)

    # ------------------------------------------------------------------ #
    # Public sync API
    # ------------------------------------------------------------------ #
    def send_message(self, data: IrsMessage | dict, opcode: int | None = None, unit_name: str | None = None) -> None:
        """
        Send `data` tagged with `opcode` to `unit_name` (or the sole
        connected unit if omitted). `opcode` is mandatory: every message
        must declare what kind of message it is so the receiving side's
        subscribe-or-drop filtering and echo handling can work.

        `opcode` goes through `tools.general.validated_opCode` first, so a
        caller may pass whatever spelling their opcode constants come in --
        an int, or the `"0x1f"`-style string a config or a UI hands over --
        and the framework sees one canonical int. That matters beyond
        convenience: `_encode` passes the opcode to `IRS.irs_to_bytes` to
        select a message layout and `framing` packs it into the uint16 header,
        and a string reaching either would be a failure well away from the
        mistake.

        `data` is an application message object; it is handed to
        `IRS.irs_parser.irs_to_bytes` to become wire bytes. Raw `bytes` are
        taken as already-encoded and sent through unchanged, so a caller that
        assembles its own payload still works.
        """
        opcode = extract_opcode(opcode)
        unit = self._resolve_unit(unit_name)
        payload = self._encode(opcode, data, unit)
        self._loop_thread.await_coroutine(self._do_send(unit, payload, opcode))

    def wait_for_connected_units(
        self, target: ConnectedTarget, timeout: float | int | None = None
    ) -> bool:
        """
        Block the calling thread until this connection's units are connected.

        `target` may be:
          * `int`  -- wait until at least this many units are connected
          * `str`  -- wait until this specific unit is connected
          * `list[str]` (any iterable of names) -- wait until all of them are

        Returns True once the condition holds, False if `timeout` expires
        first. Returns immediately if the condition is already satisfied.
        Backed by an event fired on each state change, so a waiter costs
        nothing while it waits -- there is no polling anywhere in this path.
        """
        predicate = self._build_unit_predicate(target)
        wait_timeout = timeout + 1 if timeout is not None else None
        met: bool = self._loop_thread.await_coroutine(
            self._wait_for_units(predicate, timeout), timeout=wait_timeout
        )
        return met

    def _build_unit_predicate(self, target: ConnectedTarget) -> Callable[[], bool]:
        """Validate `target` against what is actually configured, and turn it
        into the condition `_wait_for_units` re-evaluates. Unknown names and
        impossible counts fail here, in the caller's own thread, rather than
        becoming a wait that could never succeed."""
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
        async def _wait() -> None:
            # Re-reading self._state_event each pass is what makes this safe
            # for concurrent waiters: _notify_state_change swaps in a fresh
            # event, so nobody can consume another waiter's wake-up.
            while not predicate():
                await self._state_event.wait()

        if timeout is None:
            await _wait()
            return True
        try:
            await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def receive_message(self, opcode: int | str | IrsMessage, unit_name: str | None = None,
        timeout: float | int | None = None, trigger_function: TriggerFunction | None = None) -> IrsMessage:
        """
        Blocking, synchronous receive. Returns (unit_name, message) for the
        first message matching BOTH `opcode` and the resolved unit, with the
        payload already decoded by `IRS.irs_parser.parse_irs`.

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
        opcode = extract_opcode(opcode)
        unit, route_key = self._resolve_route(unit_name, opcode)
        self._validate_route(unit, route_key)
        future: asyncio.Future[IrsMessage] = self._loop_thread.call_on_loop(
            self._routes.claim, route_key, self._loop_thread.loop
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

    def handle_on_receive(self, opcode: int | str | IrsMessage,
        callback_func: ReceiveCallback, unit_name: str | None = None) -> None:
        """
        Register a standing handler for a route instead of polling it.

        Where `receive_message` subscribes, takes ONE message and unsubscribes,
        this registers `callback_func` permanently: every subsequent inbound
        message matching `opcode` and the resolved unit invokes
        `callback_func(message)` -- decoded by `IRS.irs_parser.parse_irs`, same as
        `receive_message` returns -- until `stop_on_receive()` or `close()`.
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
        opcode = extract_opcode(opcode)
        unit, route_key = self._resolve_route(unit_name, opcode)
        self._validate_route(unit, route_key)
        self._loop_thread.call_on_loop(self._routes.register_callback, route_key, callback_func)

    def stop_on_receive(self, opcode: int | str | IrsMessage, unit_name: str | None = None) -> bool:
        """
        Remove the standing callback registered for this route by
        `handle_on_receive`. Returns True if one was registered, False if
        there was nothing to remove. Callbacks already running are left to
        finish.
        """
        opcode = extract_opcode(opcode)
        _unit, route_key = self._resolve_route(unit_name, opcode)
        removed: bool = self._loop_thread.call_on_loop(self._routes.unregister_callback, route_key)
        return removed

    def handle_on_connect(
        self,
        callback_func: ConnectCallback,
        unit_name: str | None = None,
    ) -> None:
        """
        Register a standing handler, invoked the moment `unit_name` (or the
        sole connected unit, if omitted) gains a usable peer -- the
        connect-time counterpart to `handle_on_receive`, keyed by unit
        instead of by (unit, opcode) route since a connect event has no
        opcode.

        The callback receives the unit's name and runs on an executor
        thread, never the event loop -- exactly like an on-receive callback
        (see `_run_callback`) -- so it is free to call back into this
        connection's sync API, typically `send_message`, to greet a peer the
        instant it connects. An exception raised inside it is logged and
        swallowed, the same as an on-receive callback.

        A unit already carrying a callback must be released with
        `stop_on_connect()` first, same rule as `handle_on_receive`.

        If the unit is already connected when this is called, nothing fires
        retroactively -- this only arms the *next* connect, mirroring
        `wait_for_connected_units()`'s own "already true" special case being
        the caller's to check first via `active_units`.
        """
        if not callable(callback_func):
            raise TypeError(f"callback_func must be callable, got {callback_func!r}")
        unit = self._resolve_unit(unit_name)
        self._loop_thread.call_on_loop(self._routes.register_connect, unit, callback_func)

    def stop_on_connect(self, unit_name: str | None = None) -> bool:
        """
        Remove the standing callback registered for this unit by
        `handle_on_connect`. Returns True if one was registered, False if
        there was nothing to remove. A callback already running is left to
        finish.
        """
        unit = self._resolve_unit(unit_name)
        removed: bool = self._loop_thread.call_on_loop(self._routes.unregister_connect, unit)
        return removed

    def periodic_sending(
        self,
        data: IrsMessage | dict[str, Any],
        opcode: int | None,
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

        `data` goes through `IRS.irs_parser` exactly as in `send_message`, but
        is encoded once here rather than per tick: a schedule that can never
        produce a valid message fails in the caller's thread instead of
        logging the same parser error forever in the background.

        `opcode` is validated exactly as in `send_message` -- this is a send,
        just a repeating one -- so the route key this schedule is filed under
        is the same one `stop_periodic` computes from the same input.

        `unit_name` is optional when this connection has exactly one
        connected unit, matching `send_message`/`receive_message`.
        """
        opcode = extract_opcode(opcode)
        interval_seconds = float(interval)
        if interval_seconds <= 0:
            raise ValueError(f"interval must be > 0 seconds, got {interval!r}")
        unit, route_key = self._resolve_route(unit_name, opcode)
        payload = self._encode(opcode, data, unit)
        self._loop_thread.await_coroutine(self._start_periodic(unit, route_key, payload, opcode, interval_seconds))

    def stop_periodic(self, opcode: int | str | IrsMessage, unit_name: str | None = None) -> bool:
        """
        Stop the periodic sender started for this (unit, opcode) route.
        Returns True if one was running, False if there was nothing to stop.

        `opcode` is validated the same way `periodic_sending` validated it, so
        the two agree on which route is being addressed however the caller
        spelled the opcode.
        """
        opcode = extract_opcode(opcode)
        _unit, route_key = self._resolve_route(unit_name, opcode)
        stopped: bool = self._loop_thread.await_coroutine(self._stop_periodic(route_key))
        return stopped

    # -- periodic sending internals (all run on the loop thread) ---------- #
    async def _start_periodic(self, unit_name: str, route_key: RouteKey, data: bytes, opcode: int, interval: float) -> None:
        await self._stop_periodic(route_key)
        task = self._track(self._periodic_send_loop(unit_name, data, opcode, interval))
        self._periodic_tasks[route_key] = task
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
            except Exception as exc:
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
        by traffic while it is being canceled."""
        ...

    @abstractmethod
    async def _do_send(self, unit_name: str, data: Any, opcode: int) -> None:
        """Frame (if applicable) and transmit one message to `unit_name`.
        `data` is wire bytes on framed connections and a typed native sample
        on the ones that do their own serialization (DDS).
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

    These are also the connections whose payload bodies are IRS messages, so
    this is where the IRS.irs_parser codec is switched on: the header stays this
    framework's business (TCP needs DataLength to find message boundaries at
    all), and everything after it belongs to IRS.irs_parser.
    """
    uses_irs_parser = True

    def _frame(self, unit_name: str, data: bytes, opcode: int) -> bytes:
        """Build the wire bytes for one message: the 5-byte header carrying
        OUR unit code (so the peer knows who sent it), the opcode and the
        payload length, then the payload itself."""
        return pack_message(self._own_unit_code, opcode, data)
