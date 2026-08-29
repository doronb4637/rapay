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
import copy
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .delay import get_delay_queue
from .fieldpath import ABSENT, Condition, assign_path, build_condition, resolve_path
from .timing import TIMER_TICK_SECONDS, high_resolution_clock, sleep_until, wall_time

logger = logging.getLogger("gsim.behaviours")

#: WHAT MAKES A BEHAVIOUR FIRE. Orthogonal to `MODES` below -- every trigger can
#: drive either action, which is why they are two fields rather than one flat
#: list of shapes.
#:
#:   immediate    -- fire as soon as it is enabled and its connection is up.
#:                   The original (and only) behaviour shape.
#:   on_connect   -- fire when the target peer gains a usable peer.
#:   on_received  -- fire when a chosen inbound message arrives from that peer,
#:                   optionally gated on a field condition.
TRIGGERS: tuple[str, ...] = ("immediate", "on_connect", "on_received")

#: WHAT IT DOES WHEN IT FIRES.
MODE_ONCE = "once"
MODE_PERIODIC = "periodic"
MODES: tuple[str, ...] = (MODE_ONCE, MODE_PERIODIC)

#: Legacy `kind` values, mapped onto the (trigger, mode) pair they always meant.
#: `kind="periodic"` was the only one, and it is exactly immediate+periodic --
#: kept so a stored request or an external caller written against the old shape
#: keeps working without a migration.
KINDS: tuple[str, ...] = ("periodic",)
LEGACY_KINDS: dict[str, tuple[str, str]] = {"periodic": ("immediate", MODE_PERIODIC)}

#: Ceiling on a response delay. A latency is a simulation of processing time, not
#: a scheduler -- anything longer is a periodic behaviour wearing a disguise, and
#: a typo'd 60000 would otherwise queue a response nobody is still waiting for.
MAX_DELAY_MS = 60_000

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

    # -- what makes it fire ------------------------------------------------
    #: See TRIGGERS. `immediate` keeps the original meaning exactly.
    trigger: str = "immediate"
    #: What one firing does: send once, or (re)start a periodic schedule.
    mode: str = MODE_PERIODIC
    #: For `on_received`: which peer's message, and which one. `None` on the
    #: other triggers. The peer is named separately from `unit_name` because
    #: they genuinely differ -- a behaviour can watch one unit and answer
    #: another, which is what makes a gateway simulable.
    trigger_unit_name: str | None = None
    trigger_op_code: int | None = None
    #: Optional gate on the incoming payload. A `fieldpath.Condition`, shared
    #: verbatim with a received filter's rule.
    condition: Condition | None = None
    #: Response latency in milliseconds, applied before the send (or before the
    #: first tick of a periodic one). 0 means answer immediately.
    delay_ms: float = 0.0
    #: Value forwarding: `[{"from": <incoming path>, "to": <outgoing path>}]`.
    #: Applied to a COPY of `payload` on each firing, so the configured payload
    #: stays the template and a mapping can never corrupt it.
    mappings: list[dict[str, str]] = field(default_factory=list)

    # -- what it has actually done ----------------------------------------
    #: Bumped whenever a firing rewrites `payload` through `mappings`. The
    #: periodic worker watches it to know its cached `PreparedSend` is stale --
    #: which is what lets a re-triggered schedule pick up new values without
    #: being torn down and rebuilt.
    payload_version: int = 0
    #: The value each mapping most recently carried, keyed by its target path.
    #: This is what the modal renders beside the mapping row: a mapping reading
    #: an absent field looks identical to a working one until it shows you a
    #: value.
    last_mapped: dict[str, Any] = field(default_factory=dict)
    #: How many times the TRIGGER fired, as distinct from how many messages went
    #: out -- they differ by design for a periodic action (one trigger, many
    #: sends) and whenever a condition rejects an arrival.
    fired_count: int = 0
    #: Arrivals this trigger saw and declined, because its condition said no.
    #: Without it, a condition typo is indistinguishable from a silent link.
    rejected_count: int = 0
    last_fired_at: float | None = None
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
    #: Set by the engine, not persisted. For an `immediate` behaviour this is
    #: "a worker is live right now". For a reactive one it is "armed and
    #: listening" -- there is no worker until something triggers it, and a dot
    #: that only lit while a response was in flight would read as broken.
    #: `enabled` is intent; this is reality (false while the connection is down).
    active: bool = False

    @property
    def is_reactive(self) -> bool:
        return self.trigger != "immediate"

    def resolved_payload(self, incoming: dict[str, Any] | None) -> dict[str, Any]:
        """This behaviour's payload for ONE firing, with `mappings` applied.

        Returns a copy: `payload` is the configured template and must survive
        every firing unchanged, or a mapping would permanently overwrite what
        the user typed. A mapping whose source is absent from the incoming
        message is SKIPPED rather than written as null -- the template's value
        is a deliberate choice and a missing source is not a reason to discard
        it. `last_mapped` records what actually travelled, absences included.
        """
        if not self.mappings or incoming is None:
            return self.payload
        payload = copy.deepcopy(self.payload)
        seen: dict[str, Any] = {}
        for mapping in self.mappings:
            source, target = mapping.get("from"), mapping.get("to")
            if not source or not target:
                continue
            value = resolve_path(incoming, tuple(source.split(".")))
            if value is ABSENT:
                seen[target] = None
                continue
            assign_path(payload, tuple(target.split(".")), value)
            seen[target] = value
        self.last_mapped = seen
        return payload

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "connection_name": self.connection_name,
            "unit_name": self.unit_name,
            "op_code": self.op_code,
            "op_code_hex": f"0x{self.op_code:04X}",
            "kind": self.kind,
            "trigger": self.trigger,
            "mode": self.mode,
            "trigger_unit_name": self.trigger_unit_name,
            "trigger_op_code": self.trigger_op_code,
            "trigger_op_code_hex": (
                None if self.trigger_op_code is None else f"0x{self.trigger_op_code:04X}"),
            "condition": None if self.condition is None else self.condition.as_dict(),
            "delay_ms": self.delay_ms,
            "mappings": list(self.mappings),
            "last_mapped": dict(self.last_mapped),
            "fired_count": self.fired_count,
            "rejected_count": self.rejected_count,
            "last_fired_at": self.last_fired_at,
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
        is_unit_connected: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._sender = sender
        self._is_running = is_connection_running
        self._publish = publish
        #: Whether ONE peer currently has a usable link. Needed because core's
        #: `handle_on_connect` explicitly does not fire retroactively
        #: (core/connections/CLAUDE.md 5c), so arming an `on_connect` behaviour
        #: against a peer that is already up would otherwise do nothing at all
        #: -- and "nothing happened" would be the first experience of the
        #: feature. Optional so a test can build an engine without one.
        self._is_unit_connected = is_unit_connected or (lambda _conn, _unit: False)
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
    def set(self, connection_name: str, unit_name: str, op_code: int,
            payload: dict[str, Any], kind: str | None = None,
            trigger: str = "immediate", mode: str = MODE_PERIODIC,
            interval: float = 1.0, message_name: str | None = None,
            enabled: bool = True, trigger_unit_name: str | None = None,
            trigger_op_code: int | None = None,
            condition: dict[str, Any] | None = None, delay_ms: float = 0.0,
            mappings: list[dict[str, str]] | None = None) -> Behaviour:
        """Create or REPLACE the behaviour on this route AND trigger.

        Replacing rather than appending is why the key exists at all (see the
        module docstring). The key includes the trigger because two rules
        sending one message on two DIFFERENT stimuli do not conflict -- "greet
        on connect" and "answer a poll" are both legitimately about the same
        outbound message. What still collides, and must, is two `immediate`
        periodic schedules on one route: that is the silent rate-doubling the
        rule was written for.

        `kind` is the legacy spelling of the (trigger, mode) pair and wins when
        given, so a caller written before triggers existed keeps working.
        """
        if kind is not None:
            if kind not in LEGACY_KINDS:
                raise ValueError(f"unknown behaviour kind {kind!r}; known: {list(KINDS)}")
            trigger, mode = LEGACY_KINDS[kind]
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown trigger {trigger!r}; known: {list(TRIGGERS)}")
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; known: {list(MODES)}")
        if mode == MODE_PERIODIC and interval < MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"interval must be at least {MIN_INTERVAL_SECONDS}s, got {interval}")
        if not 0 <= delay_ms <= MAX_DELAY_MS:
            raise ValueError(f"delay must be between 0 and {MAX_DELAY_MS}ms, got {delay_ms}")
        if trigger == "on_received" and trigger_op_code is None:
            raise ValueError("trigger 'on_received' needs a trigger_op_code to listen for")
        if trigger != "on_received" and condition is not None:
            raise ValueError("a condition needs an incoming message to test, so it is "
                             "only meaningful with trigger 'on_received'")
        if trigger != "on_received" and mappings:
            raise ValueError("value forwarding needs an incoming message to read from, so "
                             "it is only meaningful with trigger 'on_received'")
        built_condition = build_condition(condition)

        with self._lock:
            behaviour = self._find_by_route(connection_name, unit_name, op_code, trigger,
                                            trigger_unit_name, trigger_op_code)
            if behaviour is None:
                behaviour = Behaviour(
                    id=f"bhv-{next(self._ids)}",
                    connection_name=connection_name,
                    unit_name=unit_name,
                    op_code=op_code,
                    kind=kind or trigger,
                    payload=payload,
                )
                self._behaviours[behaviour.id] = behaviour
            else:
                # Reuse the id so the UI's selection/highlight survives an edit.
                self._stop_worker(behaviour.id)
            behaviour.kind = kind or trigger
            behaviour.trigger = trigger
            behaviour.mode = mode
            behaviour.payload = payload
            behaviour.interval = interval
            behaviour.message_name = message_name
            behaviour.enabled = enabled
            behaviour.trigger_unit_name = trigger_unit_name
            behaviour.trigger_op_code = trigger_op_code
            behaviour.condition = built_condition
            behaviour.delay_ms = float(delay_ms)
            behaviour.mappings = list(mappings or [])
            # Counters describe THIS configuration. Carrying them across an edit
            # would attribute sends to a rate or a condition that never made them.
            behaviour.sent_count = 0
            behaviour.error_count = 0
            behaviour.last_error = None
            behaviour.last_sent_at = None
            behaviour.missed_ticks = 0
            behaviour.actual_hz = None
            behaviour.fired_count = 0
            behaviour.rejected_count = 0
            behaviour.last_fired_at = None
            behaviour.last_mapped = {}
            behaviour.payload_version += 1
            self._sync(behaviour)
        self._publish_all()
        self._fire_if_already_connected(behaviour)
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

    # -- trigger dispatch ------------------------------------------------
    def on_unit_connected(self, connection_name: str, unit_name: str) -> None:
        """A peer gained a usable link. Fire every `on_connect` behaviour aimed
        at it. Called from `runtime`'s on-connect callback, on a core executor
        thread."""
        for behaviour in self._armed(connection_name, "on_connect"):
            if behaviour.unit_name == unit_name:
                self._trigger(behaviour, incoming=None)

    def on_message(self, connection_name: str, unit_name: str, op_code: int,
                   payload: dict[str, Any] | None) -> None:
        """An inbound message arrived. Fire every `on_received` behaviour
        watching it.

        Called from `runtime._make_receive_callback` BEFORE the message reaches
        `_log`, so a Received filter that drops it from the console does not
        also silence the response. Filters decide what is shown; behaviours
        decide how the simulated unit acts, and coupling the two would put the
        cause of a missing reply in a completely different dialog.

        Runs on a core executor thread, once per inbound message -- up to
        ~1000/s. It must not raise: an exception here would take out the decode
        path for every later message on that route.
        """
        for behaviour in self._armed(connection_name, "on_received"):
            if behaviour.trigger_op_code != op_code:
                continue
            if behaviour.trigger_unit_name not in (None, unit_name):
                continue
            self._trigger(behaviour, incoming=payload)

    def _armed(self, connection_name: str, trigger: str) -> list[Behaviour]:
        with self._lock:
            return [
                behaviour for behaviour in self._behaviours.values()
                if behaviour.connection_name == connection_name
                and behaviour.trigger == trigger
                and behaviour.active
            ]

    def _trigger(self, behaviour: Behaviour, incoming: dict[str, Any] | None) -> None:
        """One firing: test the condition, map the values, honour the delay."""
        try:
            if behaviour.condition is not None and incoming is not None:
                if not behaviour.condition.matches(incoming):
                    behaviour.rejected_count += 1
                    return
            payload = behaviour.resolved_payload(incoming)
            behaviour.fired_count += 1
            behaviour.last_fired_at = wall_time()
            if behaviour.delay_ms > 0:
                # Never inline: this is a core executor thread inside the
                # receive callback, and sleeping here would stall the decode of
                # every later message on the route.
                get_delay_queue().call_later(
                    behaviour.delay_ms / 1000.0,
                    lambda: self._act(behaviour.id, payload),
                )
            else:
                self._act(behaviour.id, payload)
        except Exception:   # noqa: BLE001 - a bad rule must not kill the read loop
            logger.exception("behaviour %s trigger raised", behaviour.id)

    def _act(self, behaviour_id: str, payload: dict[str, Any]) -> None:
        """Do what the firing decided: send once, or (re)start the schedule.

        Re-read by id rather than closed over, because a delayed action can land
        after the behaviour was edited, disabled or deleted -- in which case
        there is nothing left to do and doing it anyway would be a message the
        user already told us not to send.
        """
        with self._lock:
            behaviour = self._behaviours.get(behaviour_id)
            if behaviour is None or not behaviour.active:
                return
            behaviour.payload = payload
            behaviour.payload_version += 1
            if behaviour.mode == MODE_PERIODIC:
                # Replace any schedule this behaviour already has, which is what
                # makes a re-triggered periodic pick up the newly mapped values.
                # Exactly the property core's `periodic_sending` offers, kept
                # here so every tick still goes through `runtime` and lands in
                # the console (see the module docstring).
                self._stop_worker(behaviour.id)
                self._start_worker(behaviour)
                behaviour.active = True
                return
        # A single send blocks on core's loop, so it happens OUTSIDE the lock --
        # holding it through a send would stall every other behaviour operation.
        self._send_once(behaviour_id)

    def _send_once(self, behaviour_id: str) -> None:
        behaviour = self._behaviours.get(behaviour_id)
        if behaviour is None:
            return
        never_stops = threading.Event()
        prepared = self._build(behaviour, never_stops)
        if prepared is not None:
            self._tick(behaviour, prepared, never_stops)
        self._publish_all()

    def _fire_if_already_connected(self, behaviour: Behaviour) -> None:
        """Fire an `on_connect` behaviour whose peer is ALREADY connected.

        Core's `handle_on_connect` arms the next transition and does not fire
        retroactively, so without this, configuring a handshake on a live link
        would sit silent until the peer happened to reconnect -- which reads as
        the feature being broken, on the very first use.
        """
        if (behaviour.trigger == "on_connect" and behaviour.active
                and self._is_unit_connected(behaviour.connection_name, behaviour.unit_name)):
            self._trigger(behaviour, incoming=None)

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
    def _find_by_route(self, connection_name: str, unit_name: str, op_code: int,
                       trigger: str, trigger_unit_name: str | None,
                       trigger_op_code: int | None) -> Behaviour | None:
        """The behaviour occupying this exact slot, if any.

        The slot is the outbound route PLUS what fires it. Two rules that send
        the same message on different stimuli are not the same behaviour and
        must not replace each other; two `immediate` periodic schedules on one
        route are, and still do.
        """
        for behaviour in self._behaviours.values():
            if (behaviour.connection_name == connection_name
                    and behaviour.unit_name == unit_name
                    and behaviour.op_code == op_code
                    and behaviour.trigger == trigger
                    and behaviour.trigger_unit_name == trigger_unit_name
                    and behaviour.trigger_op_code == trigger_op_code):
                return behaviour
        return None

    def _sync(self, behaviour: Behaviour) -> None:
        """Reconcile one behaviour with `enabled AND connection running`. Caller
        holds the lock. Idempotent -- every lifecycle path routes through here.

        `active` means two different things, deliberately:

        * an `immediate` behaviour is active when its worker is live, exactly as
          before;
        * a REACTIVE one is active when it is armed and listening. There is no
          worker until something triggers it, and a status dot that only lit
          during the instant of a response would read as broken on a rule that
          is working perfectly.

        A reactive behaviour that is mid-response keeps its worker (a periodic
        action) or has none (a single send); either way arming is what the dot
        reports, and the counters say what it has actually done.
        """
        live = behaviour.enabled and self._is_running(behaviour.connection_name)
        running = behaviour.id in self._workers
        if behaviour.is_reactive:
            # Never started eagerly -- a trigger starts it. But a behaviour that
            # is no longer live must not keep firing, so an existing worker
            # (from a periodic action) is torn down.
            if running and not live:
                self._stop_worker(behaviour.id)
            behaviour.active = live
            return
        if live and not running:
            self._start_worker(behaviour)
        elif running and not live:
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
        built_version = -1

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

            # A firing that remapped the payload invalidates the built
            # message; rebuilding is the ~160us `prepare_message` and must
            # happen only when the values genuinely changed.
            if prepared is not None and built_version != behaviour.payload_version:
                prepared = None
            if prepared is None:
                prepared = self._build(behaviour, stop)
                built_version = behaviour.payload_version

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

            if behaviour.mode == MODE_ONCE:
                # A one-shot schedule. Reached only by an `immediate` behaviour
                # in `once` mode -- a reactive one-shot never starts a worker at
                # all, it goes straight through `_send_once`.
                return

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
