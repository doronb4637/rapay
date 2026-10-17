"""
`UnitEchoSupervisor` -- the echo (heartbeat) lifecycle of one connection, keyed
by unit: peers may heartbeat on different opcodes, at different rates, or not
at all, so each unit runs on its own resolved `EchoSettings`.

Per unit, armed when it gains a peer and disarmed when it loses one (never by
`start()`, which would aim heartbeats at peers that do not exist yet):

  1. a SENDER, transmitting the echo opcode every `EchoInterval`;
  2. CONSUMPTION, which intercepts inbound echoes before subscribe-or-drop;
  3. a WATCHDOG, which drops the unit if no echo arrives within `EchoTimeout`.

A direction-limited connection gets only the half it can perform: watchdogging
a send-only member would guarantee a spurious disconnect.

Everything here runs ON the shared event-loop thread.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Coroutine, Protocol

from .config import EchoSettings

logger = logging.getLogger("connmgr")

UnitName = str


class EchoHost(Protocol):
    """
    The whole contract between the supervisor and its connection, so the echo
    lifecycle can be tested against a stub instead of a live socket pair.
    """

    can_send: bool
    can_receive: bool

    @property
    def active_units(self) -> set[str]: ...

    def _echo_for(self, unit_name: UnitName) -> EchoSettings: ...
    def _track(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]: ...
    async def _do_send(self, unit_name: str, data: Any, opcode: int) -> None: ...
    async def _disconnect_unit(self, unit_name: str, reason: str = ...) -> None: ...


class UnitEchoSupervisor:
    """Per-unit heartbeat senders, liveness clocks and timeout watchdogs."""

    __slots__ = ("_host", "_last_echo_at", "_tasks")

    def __init__(self, host: EchoHost) -> None:
        self._host = host
        #: unit name -> monotonic time of the last echo received from it.
        self._last_echo_at: dict[UnitName, float] = {}
        #: unit name -> that unit's live sender/watchdog tasks.
        self._tasks: dict[UnitName, list[asyncio.Task[None]]] = {}

    # ------------------------------------------------------------------ #
    # Arming and disarming (driven solely by per-unit connection state)
    # ------------------------------------------------------------------ #
    def arm(self, unit_name: UnitName) -> None:
        """Start `unit_name`'s heartbeat. A no-op if its echo is disabled or it is
        already armed, which keeps `_mark_unit_connected` idempotent."""
        echo = self._host._echo_for(unit_name)
        if not echo.enabled:
            return
        if any(not task.done() for task in self._tasks.get(unit_name, [])):
            return  # already armed
        # Seed liveness from the moment the peer appeared, otherwise the
        # watchdog would measure silence against the epoch and fire instantly.
        self._last_echo_at[unit_name] = time.monotonic()
        unit_tasks: list[asyncio.Task[None]] = []
        if self._host.can_send:
            unit_tasks.append(self._host._track(self._sender_loop(unit_name, echo)))
        if self._host.can_receive:
            unit_tasks.append(self._host._track(self._watchdog_loop(unit_name, echo)))
        self._tasks[unit_name] = unit_tasks

    def disarm(self, unit_name: UnitName) -> None:
        """Stop `unit_name`'s heartbeat and forget its liveness clock. The calling
        task is skipped: the watchdog reaches this via `_disconnect_unit`."""
        current = asyncio.current_task()
        for task in self._tasks.pop(unit_name, []):
            if task is not current and not task.done():
                task.cancel()
        self._last_echo_at.pop(unit_name, None)

    def forget_all(self) -> None:
        """Drop every unit's bookkeeping at teardown. The tasks are not cancelled
        here: the host's `_track` sweep cancels and awaits them."""
        self._tasks.clear()
        self._last_echo_at.clear()

    # ------------------------------------------------------------------ #
    # Consumption
    # ------------------------------------------------------------------ #
    def consume(self, unit_name: UnitName, opcode: int) -> bool:
        """
        True if this message was `unit_name`'s inbound echo, which is consumed
        here and never reaches the application.

        Only the liveness clock is refreshed, never a reply: with a shared
        `echo_opcode` the peers would answer each other's answers forever. The
        check is per unit because one opcode may be a heartbeat on a unit and an
        ordinary message on another.
        """
        echo = self._host._echo_for(unit_name)
        if not (echo.enabled and opcode == echo.recv_opcode):
            return False
        self._last_echo_at[unit_name] = time.monotonic()
        return True

    # ------------------------------------------------------------------ #
    # The two loops
    # ------------------------------------------------------------------ #
    async def _sender_loop(self, unit_name: UnitName, echo: EchoSettings) -> None:
        """Send the echo every `EchoInterval` while the unit stays connected --
        unconditionally, since it is what keeps the REMOTE watchdog quiet."""
        assert echo.send_opcode is not None
        while True:
            await asyncio.sleep(echo.interval)
            if unit_name not in self._host.active_units:
                return  # peer dropped between ticks; arm() restarts this
            try:
                await self._host._do_send(unit_name, b"", echo.send_opcode)
            except asyncio.CancelledError:
                raise
            except ConnectionError as exc:
                # The link is provably gone: retire the unit fully, exactly as
                # the watchdog would, rather than wait for it.
                logger.info("echo to unit %s undeliverable (%s); disconnecting it", unit_name, exc)
                await self._host._disconnect_unit(unit_name, f"echo undeliverable: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - a failed echo must not kill the loop
                # Not a link failure (codec/protocol): retry, and let the
                # watchdog decide when to give up.
                logger.warning("echo send to unit %s failed: %s", unit_name, exc)

    async def _watchdog_loop(self, unit_name: UnitName, echo: EchoSettings) -> None:
        """Drop the unit once its EchoTimeout passes without an echo. Sleeps to
        the deadline, not a fixed tick, so detection happens at exactly
        EchoTimeout rather than up to 2x."""
        while True:
            last_seen = self._last_echo_at.get(unit_name)
            if last_seen is None:
                return  # the unit was disarmed underneath us
            remaining = (last_seen + echo.timeout) - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            logger.warning(
                "no echo from unit %s for %.2fs (EchoTimeout=%.2fs) -- disconnecting it",
                unit_name, time.monotonic() - last_seen, echo.timeout,
            )
            await self._host._disconnect_unit(unit_name)
            return
