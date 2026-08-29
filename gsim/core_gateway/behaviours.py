"""
Behaviours: scheduled/automated sending, configured per message.

A behaviour is "keep sending THIS message to THIS peer, like THIS" -- today the
only shape is `periodic` (every N seconds), but the module is built so adding
`burst`, `send N times`, `ramp`, jitter, or per-tick payload mutation is a new
`kind` plus a branch in `_run`, not a redesign.

Two design points worth stating, because both were deliberate choices:

1. **This does NOT use core's `Connection.periodic_sending`.** Core has one, and
   it works, but its send loop calls `_do_send` directly on core's own event
   loop -- it never passes through `GSimRuntime`'s send path, so GSim would log
   nothing for a schedule that is actively producing traffic, while the
   *receiving* GSim connection would still log every single tick (its inbound
   callbacks are unaffected). The console would contradict itself: a silent
   Sent pane next to a Received pane filling up. Driving the schedule from here
   and firing it through the same `runtime.sender()` the manual button goes
   through makes every tick a normal, identical log entry.

   It also buys extensibility core's version cannot: `periodic_sending` encodes
   its payload ONCE at schedule time, so anything that varies per tick (a
   counter, a timestamp, jitter) is impossible through it by construction.

   And -- measured, after a 0.001s schedule was reported firing at 16ms --
   switching to it would not even be FASTER. Its send loop paces itself with
   `asyncio.sleep`, which on the Windows Proactor loop is pinned to the same
   15.6ms system timer tick that `threading.Event.wait` is: asking either for
   1ms on the reference machine (3.11.7 / Win10) returns after 15.5ms and
   15.3ms respectively. The bottleneck was never where the loop lived, it was
   the sleep primitive. `timing.sleep_until` is the fix; see its docstring.

2. **Behaviours are keyed by ROUTE, not by id.** `(connection_name, unit_name,
   op_code)` -- one schedule per message per destination. This mirrors the rule
   core enforces internally for `_periodic_tasks` and exists for the same
   reason: two schedules on one route would silently double its send rate, and
   nothing downstream could tell you why. Configuring a route that already has
   a behaviour REPLACES it.

A behaviour fires only while it is `enabled` (the user's intent) AND its
connection is running. Both conditions funnel through `_sync`, which is what
makes stopping a connection pause its behaviours and starting it resume them,
with no separate resume bookkeeping.
"""
from __future__ import annotations

import contextlib
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .timing import TIMER_TICK_SECONDS, high_resolution_clock, sleep_until, wall_time

logger = logging.getLogger("gsim.behaviours")

#: Every behaviour shape this module knows how to run. Adding one means adding
#: a branch in `_run` -- kept as a tuple so the API layer can validate against
#: it rather than duplicating the list.
KINDS: tuple[str, ...] = ("periodic",)

#: Floor on `periodic` interval. Not a core limit -- a guard against a typo'd
#: 0.001 turning into a tight loop that floods the console and the peer.
MIN_INTERVAL_SECONDS = 0.001

#: How often a running behaviour's counters (`sent_count`, `actual_hz`,
#: `missed_ticks`) are pushed to clients. Unrelated to the message stream,
#: which carries every entry: these are numbers a human reads off a panel,
#: and 2Hz is as fast as that is worth doing.
STATS_PERIOD_SECONDS = 0.5

#: Weight of the newest sample in `actual_hz`'s moving average. Low enough that
#: a single scheduling hiccup does not make the panel's rate readout twitch,
#: high enough that a real rate change shows up within a second at 1kHz.
RATE_SMOOTHING = 0.05


@dataclass
class Behaviour:
    id: str
    connection_name: str
    unit_name: str
    op_code: int
    kind: str
    payload: dict[str, Any]
    #: Already normalised by `prepare_message` at configure time, exactly as the
    #: manual send path does -- so a tick cannot fail on a missing field that
    #: IRS's `fill()` would have supplied. Held in `to_dict()` form, which is
    #: what the worker hands to `sender()` ONCE, when it starts; a tick fires
    #: the message built from it rather than rebuilding from this dict.
    interval: float = 1.0
    message_name: str | None = None
    enabled: bool = True
    sent_count: int = 0
    error_count: int = 0
    last_error: str | None = None
    last_sent_at: float | None = None
    #: Ticks whose deadline had already passed by the time the previous one
    #: finished. They are SKIPPED, not fired back-to-back: a simulator that
    #: bursts to catch up is lying about the traffic pattern it is meant to
    #: reproduce. Counted so the discrepancy is visible instead of silent.
    missed_ticks: int = 0
    #: Smoothed rate this behaviour is ACTUALLY achieving, from the measured
    #: gaps between sends. `interval` is the request; this is the delivery, and
    #: at intervals near the scheduler's floor the two genuinely differ.
    actual_hz: float | None = None
    #: Set by the engine, not persisted: whether a worker is live right now.
    #: `enabled` is intent; this is reality (false while the connection is down).
    active: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "connection_name": self.connection_name,
            "unit_name": self.unit_name,
            "op_code": self.op_code,
            "op_code_hex": f"0x{self.op_code:04X}",
            "kind": self.kind,
            "interval": self.interval,
            "payload": self.payload,
            "message_name": self.message_name,
            "enabled": self.enabled,
            "active": self.active,
            "sent_count": self.sent_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_sent_at": self.last_sent_at,
            "missed_ticks": self.missed_ticks,
            "actual_hz": self.actual_hz,
        }


class BehaviourEngine:
    """Owns every behaviour and the worker threads running them.

    Collaborators are injected rather than imported so this module never
    reaches back into `runtime` (which owns an instance of this):

      `sender(connection_name, unit_name, op_code, payload)` -- resolves the
          route and builds the IRS message, returning something with a
          `fire()`. It is the SAME construction the manual send button goes
          through, which is what gets each tick logged identically; a worker
          builds one when it starts and fires it every tick, so the ~160us of
          `prepare_message` stays off the hot path.
      `is_connection_running(connection_name) -> bool`
      `publish(event: dict)` -- EventBus fan-out to WebSocket clients.
    """

    def __init__(
        self,
        sender: Callable[[str, str, int, dict[str, Any]], Any],
        is_connection_running: Callable[[str], bool],
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        self._sender = sender
        self._is_running = is_connection_running
        self._publish = publish
        self._behaviours: dict[str, Behaviour] = {}
        self._workers: dict[str, threading.Event] = {}   # behaviour id -> stop flag
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._closing = threading.Event()
        self._stats = threading.Thread(
            target=self._stats_heartbeat, name="gsim-behaviour-stats", daemon=True
        )
        self._stats.start()

    # -- queries ---------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [behaviour.as_dict() for behaviour in self._behaviours.values()]

    def get(self, behaviour_id: str) -> Behaviour:
        with self._lock:
            behaviour = self._behaviours.get(behaviour_id)
        if behaviour is None:
            raise KeyError(f"no behaviour with id {behaviour_id!r}")
        return behaviour

    # -- mutation --------------------------------------------------------
    def set(self, connection_name: str, unit_name: str, op_code: int, kind: str,
            payload: dict[str, Any], interval: float = 1.0,
            message_name: str | None = None, enabled: bool = True) -> Behaviour:
        """Create or REPLACE the behaviour on this route.

        Replacing rather than appending is the whole point of keying by route
        (see module docstring): two schedules on one message would double its
        rate with nothing to explain the discrepancy.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown behaviour kind {kind!r}; known: {list(KINDS)}")
        if kind == "periodic" and interval < MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"interval must be at least {MIN_INTERVAL_SECONDS}s, got {interval}")

        with self._lock:
            existing = self._find_by_route(connection_name, unit_name, op_code)
            if existing is not None:
                # Reuse the id so the UI's selection//highlight survives an edit,
                # and reset the counters -- they describe THIS schedule, and
                # carrying them across a rate change would misreport it.
                self._stop_worker(existing.id)
                behaviour = existing
                behaviour.kind = kind
                behaviour.payload = payload
                behaviour.interval = interval
                behaviour.message_name = message_name
                behaviour.enabled = enabled
                behaviour.sent_count = 0
                behaviour.error_count = 0
                behaviour.last_error = None
                behaviour.last_sent_at = None
                behaviour.missed_ticks = 0
                behaviour.actual_hz = None
            else:
                behaviour = Behaviour(
                    id=f"bhv-{next(self._ids)}",
                    connection_name=connection_name,
                    unit_name=unit_name,
                    op_code=op_code,
                    kind=kind,
                    payload=payload,
                    interval=interval,
                    message_name=message_name,
                    enabled=enabled,
                )
                self._behaviours[behaviour.id] = behaviour
            self._sync(behaviour)
        self._publish_all()
        return behaviour

    def set_enabled(self, behaviour_id: str, enabled: bool) -> Behaviour:
        behaviour = self.get(behaviour_id)
        with self._lock:
            behaviour.enabled = enabled
            if enabled:
                # A fresh run reports fresh numbers; a stale last_error left over
                # from a previous run would read as a current failure.
                behaviour.last_error = None
            self._sync(behaviour)
        self._publish_all()
        return behaviour

    def delete(self, behaviour_id: str) -> None:
        self.get(behaviour_id)          # raises KeyError if unknown
        with self._lock:
            self._stop_worker(behaviour_id)
            self._behaviours.pop(behaviour_id, None)
        self._publish_all()

    # -- connection lifecycle hooks --------------------------------------
    def sync_connection(self, connection_name: str) -> None:
        """Re-evaluate every behaviour on one connection.

        Called by the runtime after that connection starts or stops. Because
        `_sync` reads `is_connection_running` itself, "pause on stop" and
        "resume on start" are the same call -- there is no separate paused
        state to keep in step.
        """
        with self._lock:
            affected = [
                behaviour for behaviour in self._behaviours.values()
                if behaviour.connection_name == connection_name
            ]
            for behaviour in affected:
                self._sync(behaviour)
        if affected:
            self._publish_all()

    def remove_connection(self, connection_name: str) -> None:
        """Drop every behaviour belonging to a DELETED connection -- unlike a
        stop, there is nothing left for them to resume onto."""
        with self._lock:
            doomed = [
                behaviour_id for behaviour_id, behaviour in self._behaviours.items()
                if behaviour.connection_name == connection_name
            ]
            for behaviour_id in doomed:
                self._stop_worker(behaviour_id)
                self._behaviours.pop(behaviour_id, None)
        if doomed:
            self._publish_all()

    def shutdown(self) -> None:
        self._closing.set()             # stops the stats heartbeat
        with self._lock:
            for behaviour_id in list(self._workers):
                self._stop_worker(behaviour_id)
            self._behaviours.clear()

    # -- internals -------------------------------------------------------
    def _find_by_route(self, connection_name: str, unit_name: str,
                       op_code: int) -> Behaviour | None:
        for behaviour in self._behaviours.values():
            if (behaviour.connection_name == connection_name
                    and behaviour.unit_name == unit_name
                    and behaviour.op_code == op_code):
                return behaviour
        return None

    def _sync(self, behaviour: Behaviour) -> None:
        """Make the worker match `enabled AND connection running`. Caller holds
        the lock. Idempotent -- every lifecycle path routes through here."""
        should_run = behaviour.enabled and self._is_running(behaviour.connection_name)
        running = behaviour.id in self._workers
        if should_run and not running:
            self._start_worker(behaviour)
        elif running and not should_run:
            self._stop_worker(behaviour.id)
        behaviour.active = behaviour.id in self._workers

    def _start_worker(self, behaviour: Behaviour) -> None:
        stop = threading.Event()
        self._workers[behaviour.id] = stop
        thread = threading.Thread(
            target=self._run,
            args=(behaviour.id, stop),
            name=f"gsim-behaviour-{behaviour.id}",
            daemon=True,
        )
        thread.start()

    def _stop_worker(self, behaviour_id: str) -> None:
        """Signal the worker and forget it. Deliberately does NOT join: this can
        be called from an API thread while the worker is mid-`send()`, which
        blocks on core's loop, and holding the engine lock through that would
        stall every other behaviour operation. The worker re-checks `stop`
        before each send and exits on its own; a tick already in flight is
        allowed to finish, which is correct -- it is a real message."""
        stop = self._workers.pop(behaviour_id, None)
        if stop is not None:
            stop.set()
        behaviour = self._behaviours.get(behaviour_id)
        if behaviour is not None:
            behaviour.active = False

    def _run(self, behaviour_id: str, stop: threading.Event) -> None:
        """One behaviour's worker thread.

        A schedule at or below the Windows timer tick holds
        `high_resolution_clock()` for the worker's whole lifetime -- scoped
        here, rather than for the life of the process, because it raises a
        SYSTEM-wide setting and there is no reason to hold it while GSim sits
        idle. Slower schedules do not need it and do not take it.
        """
        behaviour = self._behaviours.get(behaviour_id)
        if behaviour is None:
            return
        fast = behaviour.interval < TIMER_TICK_SECONDS
        with (high_resolution_clock() if fast else contextlib.nullcontext()):
            self._pace(behaviour_id, stop)

    def _pace(self, behaviour_id: str, stop: threading.Event) -> None:
        """The scheduling loop: fire on ABSOLUTE deadlines, not `sleep(interval)`.

        Pacing with a fixed sleep AFTER the work makes the real period
        `interval + work`, which for a 1ms schedule doing ~250us of encoding,
        sending and logging is a 20% rate error before any jitter enters. Each
        deadline is instead `previous + interval`, so the work is absorbed and
        the rate is the requested one.

        Reads the Behaviour fresh each pass rather than closing over its values,
        so a hypothetical edit that keeps the same route is picked up without
        the thread being torn down. `prepared` is built lazily and kept: it is
        the ~160us of `prepare_message` that must not happen per tick, and
        building it here rather than at configure time means a transient
        failure is retried on the next tick exactly as a failing send is.
        """
        clock = time.perf_counter
        deadline = clock()              # first tick fires immediately, as before
        last_fired_at: float | None = None
        prepared: Any = None

        # `actual_hz` belongs to whichever worker is LIVE, and this is it. It is
        # re-seeded here, not only in `set()`, because `_stop_worker` signals the
        # outgoing worker without joining it: its final sample can land after
        # `set()` has already reset the average, and the new worker then spends
        # ~60 ticks dragging a stale figure down. Editing a 1ms schedule to 0.5s
        # showed 550 Hz for half a minute that way.
        starting = self._behaviours.get(behaviour_id)
        if starting is not None:
            starting.actual_hz = None

        while not stop.is_set():
            if sleep_until(deadline, stop):
                return
            behaviour = self._behaviours.get(behaviour_id)
            if behaviour is None:
                return
            interval = max(float(behaviour.interval), MIN_INTERVAL_SECONDS)

            if prepared is None:
                prepared = self._build(behaviour, stop)

            if prepared is not None:
                fired_at = clock()
                # Every tick is sent, counted AND streamed -- `runtime._log`
                # publishes unconditionally. Rate-limiting the stream here (or
                # anywhere) was tried and reverted: it made the console report
                # the sampling period instead of the send period, which is the
                # one thing the console must never do. Volume is handled by
                # batching in `api/routes/events.py` instead.
                if self._tick(behaviour, prepared, stop):
                    if last_fired_at is not None:
                        self._observe_rate(behaviour, fired_at - last_fired_at)
                    last_fired_at = fired_at
                else:
                    # A failed send is transient (peer not up yet) and does not
                    # invalidate the built message, so `prepared` is KEPT --
                    # rebuilding it every tick through an outage would pay the
                    # 160us for nothing. Anything that could actually invalidate
                    # it (stop, edit, delete) tears this worker down instead.
                    last_fired_at = None     # the gap across a failure is not a rate

            now = clock()
            deadline += interval
            if deadline <= now:
                # Overran. Skip the missed ticks and realign to the next whole
                # deadline rather than firing back-to-back to catch up: bursting
                # misrepresents the traffic pattern the schedule exists to
                # reproduce. The count is what makes the shortfall visible.
                missed = int((now - deadline) // interval) + 1
                behaviour.missed_ticks += missed
                deadline += missed * interval

    def _build(self, behaviour: Behaviour, stop: threading.Event) -> Any:
        """Resolve the route and build the message, or record why it could not
        be. Returns None on failure; the caller retries on the next tick."""
        try:
            return self._sender(behaviour.connection_name, behaviour.unit_name,
                                behaviour.op_code, behaviour.payload)
        except Exception as exc:  # noqa: BLE001 -- surfaced on the behaviour, not raised
            self._record_error(behaviour, exc, stop)
            return None

    def _tick(self, behaviour: Behaviour, prepared: Any, stop: threading.Event) -> bool:
        """Send one message; True if it went out. Never raises -- a failing tick
        must not kill the schedule, because the usual cause (peer not connected
        yet) is transient and the behaviour should still be firing when it
        recovers."""
        try:
            prepared.fire()
        except Exception as exc:  # noqa: BLE001 -- surfaced on the behaviour, not raised
            self._record_error(behaviour, exc, stop)
            return False

        if stop.is_set():
            # Torn down mid-send. The message really went out, but this worker
            # no longer owns the counters -- a replacement may already have
            # reset them, and writing now would corrupt ITS numbers rather than
            # correct ours.
            return False
        behaviour.sent_count += 1
        behaviour.last_sent_at = wall_time()
        if behaviour.last_error is not None:
            behaviour.last_error = None     # recovered
            self._publish_all()
        return True

    def _record_error(self, behaviour: Behaviour, exc: Exception,
                      stop: threading.Event) -> None:
        if stop.is_set():
            return              # torn down mid-send; not a real failure
        message = f"{type(exc).__name__}: {exc}"
        first = behaviour.last_error != message
        behaviour.error_count += 1
        behaviour.last_error = message
        if first:
            # Publish only on a CHANGE of error state, never per tick -- a 20ms
            # schedule failing would otherwise flood every WebSocket client with
            # identical updates.
            logger.warning("behaviour %s send failed: %s", behaviour.id, message)
            self._publish_all()

    @staticmethod
    def _observe_rate(behaviour: Behaviour, delta: float) -> None:
        """Fold one measured gap into `actual_hz`. Smoothed rather than
        instantaneous so the panel shows the rate the link is sustaining, not
        whatever the last two ticks happened to do."""
        if delta <= 0:
            return
        sample = 1.0 / delta
        if behaviour.actual_hz is None:
            behaviour.actual_hz = sample
        else:
            behaviour.actual_hz += RATE_SMOOTHING * (sample - behaviour.actual_hz)

    def _stats_heartbeat(self) -> None:
        """Push running behaviours' counters to clients on a timer.

        `_publish_all` is otherwise called only when something CHANGES --
        configure, enable, an error appearing or clearing. For a slow schedule
        that is enough: each tick sends its own `message.sent` and the panel's
        numbers are refreshed alongside it. A fast one would otherwise carry its
        counters only as often as it is reconfigured -- `sent_count` /
        `actual_hz` / `missed_ticks` sitting visibly frozen while it fired
        thousands of times.
        """
        while not self._closing.wait(STATS_PERIOD_SECONDS):
            with self._lock:
                busy = bool(self._workers)
            if busy:
                self._publish_all()

    def _publish_all(self) -> None:
        """One event carrying the whole list. Cheap (behaviours are few) and it
        makes the client's job a replace rather than a merge, so a dropped or
        out-of-order event cannot leave the panel showing a stale schedule."""
        self._publish({"type": "behaviours", "behaviours": self.list()})
