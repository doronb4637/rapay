"""
Behaviours: scheduled/automated sending, configured per message.

A behaviour is "keep sending THIS message to THIS peer, like THIS" -- today the
only shape is `periodic` (every N seconds), but the module is built so adding
`burst`, `send N times`, `ramp`, jitter, or per-tick payload mutation is a new
`kind` plus a branch in `_run`, not a redesign.

Two design points worth stating, because both were deliberate choices:

1. **This does NOT use core's `Connection.periodic_sending`.** Core has one, and
   it works, but its send loop calls `_do_send` directly on core's own event
   loop -- it never passes through `GSimRuntime.send()`, so GSim would log
   nothing for a schedule that is actively producing traffic, while the
   *receiving* GSim connection would still log every single tick (its inbound
   callbacks are unaffected). The console would contradict itself: a silent
   Sent pane next to a Received pane filling up. Driving the schedule from here
   and calling the same `send()` the manual button calls makes every tick a
   normal, identical log entry.

   It also buys extensibility core's version cannot: `periodic_sending` encodes
   its payload ONCE at schedule time, so anything that varies per tick (a
   counter, a timestamp, jitter) is impossible through it by construction.

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

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("gsim.behaviours")

#: Every behaviour shape this module knows how to run. Adding one means adding
#: a branch in `_run` -- kept as a tuple so the API layer can validate against
#: it rather than duplicating the list.
KINDS: tuple[str, ...] = ("periodic",)

#: Floor on `periodic` interval. Not a core limit -- a guard against a typo'd
#: 0.001 turning into a tight loop that floods the console and the peer.
MIN_INTERVAL_SECONDS = 0.001


@dataclass
class Behaviour:
    id: str
    connection_name: str
    unit_name: str
    op_code: int
    kind: str
    payload: dict[str, Any]
    #: Already normalised by `build_payload` at configure time, exactly as the
    #: manual send path does -- so a tick cannot fail on a missing field that
    #: the Inspector would have zero-filled.
    interval: float = 1.0
    message_name: str | None = None
    enabled: bool = True
    sent_count: int = 0
    error_count: int = 0
    last_error: str | None = None
    last_sent_at: float | None = None
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
        }


class BehaviourEngine:
    """Owns every behaviour and the worker threads running them.

    Collaborators are injected rather than imported so this module never
    reaches back into `runtime` (which owns an instance of this):

      `send(connection_name, unit_name, op_code, payload)` -- the SAME call the
          manual send button makes, which is what gets each tick logged.
      `is_connection_running(connection_name) -> bool`
      `publish(event: dict)` -- EventBus fan-out to WebSocket clients.
    """

    def __init__(
        self,
        send: Callable[[str, str, int, dict[str, Any]], Any],
        is_connection_running: Callable[[str], bool],
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        self._send = send
        self._is_running = is_connection_running
        self._publish = publish
        self._behaviours: dict[str, Behaviour] = {}
        self._workers: dict[str, threading.Event] = {}   # behaviour id -> stop flag
        self._lock = threading.RLock()
        self._ids = itertools.count(1)

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

        Reads the Behaviour fresh each tick rather than closing over its values,
        so an edit that keeps the same route is picked up without the thread
        having to be torn down -- and re-checks `stop` before every send so a
        cancel during a slow send takes effect at the next boundary.
        """
        while not stop.is_set():
            behaviour = self._behaviours.get(behaviour_id)
            if behaviour is None:
                return

            self._tick(behaviour, stop)
            if stop.is_set():
                return
            # Cancellable sleep: `wait` returns True the moment stop is set,
            # so a 60s interval still stops instantly instead of after a minute.
            if stop.wait(behaviour.interval):
                return

    def _tick(self, behaviour: Behaviour, stop: threading.Event) -> None:
        """Send one message. Never raises -- a failing tick must not kill the
        schedule, because the usual cause (peer not connected yet) is transient
        and the behaviour should still be firing when it recovers."""
        try:
            self._send(behaviour.connection_name, behaviour.unit_name,
                       behaviour.op_code, behaviour.payload)
        except Exception as exc:  # noqa: BLE001 -- surfaced on the behaviour, not raised
            if stop.is_set():
                return          # torn down mid-send; not a real failure
            message = f"{type(exc).__name__}: {exc}"
            first = behaviour.last_error != message
            behaviour.error_count += 1
            behaviour.last_error = message
            if first:
                # Publish only on a CHANGE of error state, never per tick -- a
                # 20ms schedule failing would otherwise flood every WebSocket
                # client with identical updates.
                logger.warning("behaviour %s send failed: %s", behaviour.id, message)
                self._publish_all()
            return

        behaviour.sent_count += 1
        behaviour.last_sent_at = time.time()
        if behaviour.last_error is not None:
            behaviour.last_error = None     # recovered
            self._publish_all()

    def _publish_all(self) -> None:
        """One event carrying the whole list. Cheap (behaviours are few) and it
        makes the client's job a replace rather than a merge, so a dropped or
        out-of-order event cannot leave the panel showing a stale schedule."""
        self._publish({"type": "behaviours", "behaviours": self.list()})
