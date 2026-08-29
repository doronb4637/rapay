"""
One thread that runs callbacks later, for every delayed response in the process.

A reactive behaviour may declare a response latency -- "reply 50ms after the
request arrives" -- and something has to hold that gap. The obvious
implementation, a `threading.Timer` per firing, is the one that cannot be used:
an `on_received` trigger on a fast route fires once per inbound message, and
this project routinely runs routes at 1kHz. That is a thousand thread creations
a second for work measured in microseconds.

So: one daemon thread, a heap ordered by due time, and a condition variable it
sleeps on. Scheduling is O(log n) and costs no thread at all.

**Sub-tick delays are real delays.** `Condition.wait(timeout)` bottoms out on the
same Windows 15.6ms timer tick that `threading.Event.wait` does (see
`timing.py`), so a plain implementation would turn "wait 5ms" into 16ms and
"wait 1ms" into 16ms -- the exact class of bug `timing.py` exists to fix, in a
new place. The runner therefore closes the last stretch with `timing.sleep_until`
and holds `timing.high_resolution_clock()` for as long as anything sub-tick is
pending, which is also why that context is refcounted.

The queue is bounded. A trigger whose delay is longer than the interval between
the messages that fire it produces pending entries faster than they retire, and
without a cap that is an unbounded list of closures on a memory-resident
process. Past the cap the OLDEST pending entry is dropped -- the newest response
is the one that reflects current state, and the caller is told so it can count
what it lost rather than losing it silently.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from typing import Any, Callable

from .timing import TIMER_TICK_SECONDS, high_resolution_clock, sleep_until

logger = logging.getLogger("gsim.delay")

#: How many delayed callbacks may be pending before the oldest is dropped.
#: Generous -- this is a developer tool and memory is explicitly not the
#: constraint -- but finite, because "unbounded" is not a policy.
MAX_PENDING = 4096


class DelayQueue:
    """Run callables after a delay, on one shared thread.

    Callbacks run ON that thread, one at a time, in due order. They must not
    block for long: a slow callback delays every later one. Behaviour firings
    satisfy this -- the actual send is handed to core's own loop.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, Callable[[], Any]]] = []
        self._sequence = itertools.count()
        self._condition = threading.Condition(threading.Lock())
        self._closing = False
        #: How many entries were discarded because the queue was full. Read by
        #: callers that want to report it rather than hide it.
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="gsim-delay", daemon=True)
        self._thread.start()

    def call_later(self, delay_seconds: float, callback: Callable[[], Any]) -> bool:
        """Schedule `callback`. Returns False if the queue was full and this
        displaced an older entry. A delay of zero or less runs it on the next
        pass rather than inline, so the caller -- typically a core executor
        thread inside a receive callback -- is never made to wait for it."""
        due = time.perf_counter() + max(0.0, float(delay_seconds))
        accepted = True
        with self._condition:
            if len(self._heap) >= MAX_PENDING:
                heapq.heappop(self._heap)
                self.dropped += 1
                accepted = False
            heapq.heappush(self._heap, (due, next(self._sequence), callback))
            self._condition.notify()
        return accepted

    def shutdown(self) -> None:
        with self._condition:
            self._closing = True
            self._heap.clear()
            self._condition.notify()

    # -- internals -------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._heap and not self._closing:
                    self._condition.wait()
                if self._closing:
                    return
                due, _, callback = self._heap[0]
                remaining = due - time.perf_counter()
                if remaining > TIMER_TICK_SECONDS:
                    # Far enough out that the tick's granularity is noise. Wait
                    # on the condition so a nearer entry scheduled meanwhile
                    # wakes us immediately instead of after this whole sleep.
                    self._condition.wait(remaining - TIMER_TICK_SECONDS)
                    continue
                heapq.heappop(self._heap)

            # Released the lock before the precise part: `sleep_until` may spin
            # for up to a tick, and holding the lock through that would block
            # every `call_later` on a core executor thread for the same window.
            if due - time.perf_counter() > 0:
                with high_resolution_clock():
                    sleep_until(due)
            try:
                callback()
            except Exception:       # noqa: BLE001 - one bad callback must not end the thread
                logger.exception("delayed callback raised")


_queue: DelayQueue | None = None
_queue_lock = threading.Lock()


def get_delay_queue() -> DelayQueue:
    """The process-wide queue. Created on first use rather than at import, so a
    process that never schedules a delayed response never starts the thread."""
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = DelayQueue()
        return _queue
