"""
Sub-millisecond pacing on Windows, behind two names.

This module exists because of one measured fact: **`threading.Event.wait` and
`asyncio.sleep` cannot go below the Windows system timer tick.** Both end up in
`WaitForSingleObjectEx`, whose timeout is rounded up to the current tick --
15.6 ms by default. Asking either for 1 ms on this machine (Python 3.11.7,
Win10) gets you 15.3 ms and 15.5 ms respectively, and it does not matter what
you are sleeping *between*: a `periodic` behaviour on an empty message and one
on a 35-struct message both came out at 16 ms, because the sleep was the whole
number.

`time.sleep` is the one exception in the stdlib. Since CPython 3.11 it uses a
high-resolution waitable timer (`CreateWaitableTimerExW` with
`CREATE_WAITABLE_TIMER_HIGH_RESOLUTION`) rather than the tick, so it is
unaffected by the system tick -- but it still overshoots by up to ~1.4 ms.
`sleep_until` therefore sleeps most of the way and closes the last couple of
milliseconds with a yielding spin, which measured p99 = 0.5 us against an
absolute deadline.

The spin yields with `time.sleep(0)` rather than looping on `perf_counter()`
alone. That is not politeness: a bare Python loop holds the GIL for a whole
switch interval at a time, which would starve the very threads this module is
waiting on -- core's event loop thread (every send marshals onto it) and
uvicorn's. `time.sleep(0)` drops the GIL and yields the core on every pass.

GSim is a developer tool, and a behaviour asking for 1 ms is asking to saturate
a link on purpose, so burning a core to get there is the right trade. Nothing
outside `behaviours.py` uses this.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator

#: Handed back from `stop.wait()` this far out from the deadline, so that a
#: wait which returns a full timer tick late still lands *before* it. Must stay
#: comfortably above 15.6 ms for that to hold.
COARSE_SLACK_SECONDS = 0.020

#: `time.sleep` overshoots its request by up to ~1.4 ms (measured min 0.005 ms,
#: max 1.39 ms over 200 samples at each of 0.2/0.5/1/2/5 ms). Stopping this far
#: short and spinning the remainder is what keeps the overshoot off the
#: deadline.
SLEEP_SLACK_SECONDS = 0.002

#: Below this, `Event.wait` is not merely imprecise but useless -- it is the
#: Windows timer tick itself. Callers use it to decide whether a schedule needs
#: `high_resolution_clock()` at all.
TIMER_TICK_SECONDS = 0.0156

#: Held while `high_resolution_clock()` is in scope. 5 ms (the default) means a
#: CPU-bound thread -- uvicorn serialising WebSocket frames, say -- can hold the
#: GIL straight through a 1 ms deadline.
FAST_SWITCH_INTERVAL = 0.001

#: Wall clock anchored to `perf_counter` once, at import. See `wall_time()`.
_ANCHOR_WALL = time.time()
_ANCHOR_PERF = time.perf_counter()


def wall_time() -> float:
    """`time.time()`, but with sub-millisecond resolution.

    `time.time()` on Windows is `GetSystemTimeAsFileTime`, whose granularity is
    the SAME system timer tick everything else in this module is about --
    `time.get_clock_info('time').resolution` reports 0.015625 outright. It reads
    as 1 ms while anything on the machine holds `timeBeginPeriod(1)` (a browser,
    a media player, or `high_resolution_clock()` below) and 15.6 ms otherwise,
    which is not a property to build a measurement on either way.

    That matters here because `LogEntry.timestamp` is what the console renders
    and what anyone diagnosing a schedule diffs consecutive entries with: a
    perfectly paced 1 ms behaviour logged with `time.time()` still reads as 16 ms
    steps, and the timing bug looks unfixed. Anchoring a wall-clock reading to
    `perf_counter` once and adding the monotonic delta keeps the value a real
    time-of-day (which the UI formats) while giving it `perf_counter`'s
    resolution, and makes consecutive entries monotonic into the bargain -- which
    raw `time.time()`, being adjustable, is not.

    The two clocks drift slowly apart; over a GSim session that is far below
    what a console displays, and nothing here is a time source of record.
    """
    return _ANCHOR_WALL + (time.perf_counter() - _ANCHOR_PERF)


def sleep_until(deadline: float, stop: threading.Event | None = None) -> bool:
    """Block until `time.perf_counter()` reaches `deadline`.

    `deadline` is in `perf_counter` space -- an ABSOLUTE instant, not a
    duration. That is the point: pacing a loop with `sleep(interval)` after the
    work makes the real period `interval + work`, which at 1 ms is a 20% rate
    error before any jitter. Aiming at successive absolute deadlines absorbs the
    work instead.

    `stop` makes a long wait cancellable: it is waited on for everything except
    the last `COARSE_SLACK_SECONDS`, so a 60 s schedule still tears down
    instantly rather than a minute later. Returns True if `stop` was set (the
    caller should give up), False on a normal wake. Already-past deadlines
    return immediately.
    """
    remaining = deadline - time.perf_counter()
    if stop is not None and remaining > COARSE_SLACK_SECONDS:
        if stop.wait(remaining - COARSE_SLACK_SECONDS):
            return True

    # Coarse: `time.sleep` is high-resolution since 3.11 but overshoots, so stop
    # short of the deadline and let the spin below finish the job.
    remaining = deadline - time.perf_counter()
    if remaining > SLEEP_SLACK_SECONDS:
        time.sleep(remaining - SLEEP_SLACK_SECONDS)

    # Fine: yielding spin. See the module docstring for why `time.sleep(0)`.
    while time.perf_counter() < deadline:
        time.sleep(0)

    return bool(stop is not None and stop.is_set())


class _HighResolutionClock:
    """Refcount around the two process-wide knobs, so N concurrent fast
    behaviours acquire once and the last one out restores what was there."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._depth = 0
        self._previous_switch_interval: float | None = None
        # Absent on non-Windows, and `WinDLL` itself can fail in a stripped
        # environment. Either way the spin in `sleep_until` still works; only
        # the OS-wide tick stays coarse, which affects everything EXCEPT us.
        self._winmm = None
        if sys.platform == "win32":
            try:
                self._winmm = ctypes.WinDLL("winmm")
            except OSError:
                self._winmm = None

    def acquire(self) -> None:
        with self._lock:
            self._depth += 1
            if self._depth > 1:
                return
            if self._winmm is not None:
                self._winmm.timeBeginPeriod(1)
            self._previous_switch_interval = sys.getswitchinterval()
            sys.setswitchinterval(FAST_SWITCH_INTERVAL)

    def release(self) -> None:
        with self._lock:
            self._depth -= 1
            if self._depth > 0:
                return
            self._depth = 0
            if self._winmm is not None:
                self._winmm.timeEndPeriod(1)
            if self._previous_switch_interval is not None:
                sys.setswitchinterval(self._previous_switch_interval)
                self._previous_switch_interval = None


_CLOCK = _HighResolutionClock()


@contextmanager
def high_resolution_clock() -> Iterator[None]:
    """Raise the process's timing precision for as long as this is held.

    Two knobs, both process-wide, both refcounted so nesting and concurrent
    holders are safe:

    * `timeBeginPeriod(1)` -- takes the Windows timer tick from 15.6 ms to 1 ms.
      `sleep_until` does not need it (its spin already beats the tick), but
      everything else in the process does: core's asyncio loop timers, the echo
      watchdog, and every `Event.wait` GSim itself still uses. It is a SYSTEM
      setting while held, which is why this is scoped to an actually-running
      fast schedule rather than taken for the life of the process.
    * `sys.setswitchinterval(0.001)` -- caps how long another thread can hold
      the GIL against us. Trades throughput for latency, which is the right way
      round here.
    """
    _CLOCK.acquire()
    try:
        yield
    finally:
        _CLOCK.release()
