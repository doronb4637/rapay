"""
connections/_echo.py -- `UnitEchoSupervisor`: per-unit heartbeat sender,
liveness clock and timeout watchdog.

Driven against a stub `EchoHost` on a private event loop rather than a real
socket pair, so the timings can be milliseconds instead of seconds and the
questions can be exact ones ("did it send to THIS unit on THIS opcode") rather
than "did the link survive". The suite's socket-level echo tests
(`test_echo.py`) still cover the same lifecycle end to end.
"""
import asyncio

import pytest

from core.connections._echo import UnitEchoSupervisor
from core.connections.config import EchoSettings

RECV_OPCODE, SEND_OPCODE = 10, 11
#: Fast enough to keep the suite quick, slow enough that a few ticks fit in a
#: window without the scheduler's jitter deciding the outcome.
INTERVAL, TIMEOUT = 0.02, 0.12

ECHO = EchoSettings(recv_opcode=RECV_OPCODE, send_opcode=SEND_OPCODE,
                    interval=INTERVAL, timeout=TIMEOUT)
DISABLED = EchoSettings()


class StubHost:
    """The whole `EchoHost` contract, and nothing else -- which is the point of
    having written that contract down."""

    def __init__(self, loop, echo=ECHO, can_send=True, can_receive=True):
        self.loop = loop
        self.echo = echo
        self.can_send = can_send
        self.can_receive = can_receive
        self._active: set[str] = set()
        self.sent: list[tuple[str, bytes, int]] = []
        self.marked_down: list[str] = []
        self.disconnected: list[str] = []
        self.tracked: list[asyncio.Task] = []
        #: Set to an exception to make the next _do_send raise it.
        self.send_error: BaseException | None = None

    @property
    def active_units(self) -> set[str]:
        return set(self._active)

    def connect(self, unit_name: str) -> None:
        self._active.add(unit_name)

    def _echo_for(self, unit_name):
        return self.echo

    def _track(self, coro):
        task = self.loop.create_task(coro)
        self.tracked.append(task)
        return task

    async def _do_send(self, unit_name, data, opcode):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((unit_name, data, opcode))

    def _mark_unit_disconnected(self, unit_name):
        self._active.discard(unit_name)
        self.marked_down.append(unit_name)

    async def _disconnect_unit(self, unit_name):
        self._active.discard(unit_name)
        self.disconnected.append(unit_name)


@pytest.fixture
def loop():
    new_loop = asyncio.new_event_loop()
    try:
        yield new_loop
    finally:
        pending = asyncio.all_tasks(new_loop)
        for task in pending:
            task.cancel()
        if pending:
            new_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        new_loop.close()


def spin(loop, seconds: float) -> None:
    """Let the supervisor's tasks actually run for `seconds`."""
    loop.run_until_complete(asyncio.sleep(seconds))


def on_loop(loop, func, *args):
    """Call `func` the way a connection does -- ON the loop thread.

    `disarm` skips cancelling `asyncio.current_task()`, so it can only be
    invoked from there; calling it off-loop is a caller error, not something to
    paper over.
    """
    async def _call():
        return func(*args)

    return loop.run_until_complete(_call())


# --------------------------------------------------------------------------- #
# Arming
# --------------------------------------------------------------------------- #
def test_arming_a_duplex_unit_starts_a_sender_and_a_watchdog(loop):
    host = StubHost(loop)
    host.connect("Peer")
    UnitEchoSupervisor(host).arm("Peer")
    assert len(host.tracked) == 2


def test_a_send_only_connection_is_never_watchdogged(loop):
    """It has no way to receive an echo, so a watchdog would guarantee a
    spurious disconnect."""
    host = StubHost(loop, can_receive=False)
    host.connect("Peer")
    UnitEchoSupervisor(host).arm("Peer")

    spin(loop, TIMEOUT * 2)
    assert host.sent, "a send-capable unit should still heartbeat"
    assert host.disconnected == []


def test_a_receive_only_connection_never_heartbeats(loop):
    host = StubHost(loop, can_send=False)
    host.connect("Peer")
    UnitEchoSupervisor(host).arm("Peer")

    spin(loop, INTERVAL * 3)
    assert host.sent == []


def test_a_unit_whose_echo_does_not_resolve_stays_silent(loop):
    host = StubHost(loop, echo=DISABLED)
    host.connect("Peer")
    UnitEchoSupervisor(host).arm("Peer")
    assert host.tracked == []


def test_arming_twice_does_not_double_the_heartbeat(loop):
    host = StubHost(loop)
    host.connect("Peer")
    supervisor = UnitEchoSupervisor(host)
    supervisor.arm("Peer")
    supervisor.arm("Peer")
    assert len(host.tracked) == 2  # not four


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def test_the_sender_uses_this_units_send_opcode_and_an_empty_body(loop):
    host = StubHost(loop)
    host.connect("Peer")
    UnitEchoSupervisor(host).arm("Peer")

    spin(loop, INTERVAL * 4)

    assert host.sent, "no heartbeat was sent"
    assert all(sent == ("Peer", b"", SEND_OPCODE) for sent in host.sent), host.sent


def test_an_undeliverable_echo_retires_the_unit_on_the_spot(loop):
    """A ConnectionError is proof the link is gone, so the sender does not wait
    for the watchdog to reach the same conclusion."""
    host = StubHost(loop)
    host.connect("Peer")
    host.send_error = ConnectionError("no peer")
    UnitEchoSupervisor(host).arm("Peer")

    spin(loop, INTERVAL * 3)

    assert host.marked_down == ["Peer"]


def test_a_non_link_send_failure_only_costs_that_tick(loop):
    """A codec/protocol error is not proof of anything about the link: log it,
    keep the loop alive, and let the watchdog decide."""
    host = StubHost(loop)
    host.connect("Peer")
    host.send_error = ValueError("bad payload")
    UnitEchoSupervisor(host).arm("Peer")

    spin(loop, INTERVAL * 3)
    assert host.marked_down == []

    host.send_error = None
    spin(loop, INTERVAL * 3)
    assert host.sent, "the sender did not recover after a transient failure"


def test_the_sender_stops_when_the_unit_goes_down_between_ticks(loop):
    host = StubHost(loop)
    host.connect("Peer")
    UnitEchoSupervisor(host).arm("Peer")
    spin(loop, INTERVAL * 3)

    host._active.discard("Peer")  # dropped without disarming, e.g. by a read loop
    settled = len(host.sent)
    spin(loop, INTERVAL * 4)

    assert len(host.sent) == settled


# --------------------------------------------------------------------------- #
# Consumption
# --------------------------------------------------------------------------- #
def test_consume_claims_the_receive_opcode_and_nothing_else(loop):
    supervisor = UnitEchoSupervisor(StubHost(loop))
    assert supervisor.consume("Peer", RECV_OPCODE) is True
    assert supervisor.consume("Peer", SEND_OPCODE) is False
    assert supervisor.consume("Peer", 999) is False


def test_consume_claims_nothing_when_echo_is_disabled(loop):
    """An opcode that is a heartbeat on one unit stays an ordinary application
    message on a unit that configured no echo."""
    supervisor = UnitEchoSupervisor(StubHost(loop, echo=DISABLED))
    assert supervisor.consume("Peer", RECV_OPCODE) is False


# --------------------------------------------------------------------------- #
# The watchdog
# --------------------------------------------------------------------------- #
def test_the_watchdog_disconnects_a_silent_unit(loop):
    host = StubHost(loop, can_send=False)  # nothing to keep the peer honest
    host.connect("Peer")
    UnitEchoSupervisor(host).arm("Peer")

    spin(loop, TIMEOUT * 2)

    assert host.disconnected == ["Peer"]


def test_an_inbound_echo_pushes_the_deadline_out(loop):
    host = StubHost(loop, can_send=False)
    host.connect("Peer")
    supervisor = UnitEchoSupervisor(host)
    supervisor.arm("Peer")

    # Keep answering across more than one full timeout window.
    for _ in range(6):
        spin(loop, TIMEOUT / 3)
        supervisor.consume("Peer", RECV_OPCODE)
    assert host.disconnected == [], "a responsive unit was dropped"

    spin(loop, TIMEOUT * 2)  # now go quiet
    assert host.disconnected == ["Peer"]


def test_the_watchdog_stops_when_the_unit_is_disarmed(loop):
    host = StubHost(loop, can_send=False)
    host.connect("Peer")
    supervisor = UnitEchoSupervisor(host)
    supervisor.arm("Peer")

    on_loop(loop, supervisor.disarm, "Peer")
    spin(loop, TIMEOUT * 2)

    assert host.disconnected == []


# --------------------------------------------------------------------------- #
# Disarming and teardown
# --------------------------------------------------------------------------- #
def test_disarm_cancels_the_units_tasks_and_forgets_its_clock(loop):
    host = StubHost(loop)
    host.connect("Peer")
    supervisor = UnitEchoSupervisor(host)
    supervisor.arm("Peer")

    on_loop(loop, supervisor.disarm, "Peer")
    spin(loop, 0.01)

    assert all(task.cancelled() for task in host.tracked), host.tracked
    assert "Peer" not in supervisor._last_echo_at


def test_disarm_only_touches_the_named_unit(loop):
    host = StubHost(loop)
    host.connect("PeerA")
    host.connect("PeerB")
    supervisor = UnitEchoSupervisor(host)
    supervisor.arm("PeerA")
    supervisor.arm("PeerB")

    on_loop(loop, supervisor.disarm, "PeerA")
    spin(loop, INTERVAL * 4)

    assert all(unit == "PeerB" for unit, _data, _opcode in host.sent), host.sent


def test_a_unit_can_be_rearmed_after_being_disarmed(loop):
    host = StubHost(loop)
    host.connect("Peer")
    supervisor = UnitEchoSupervisor(host)
    supervisor.arm("Peer")
    on_loop(loop, supervisor.disarm, "Peer")

    supervisor.arm("Peer")
    spin(loop, INTERVAL * 4)

    assert host.sent, "the heartbeat did not restart for a reconnected peer"


def test_forget_all_clears_every_units_bookkeeping(loop):
    host = StubHost(loop)
    host.connect("Peer")
    supervisor = UnitEchoSupervisor(host)
    supervisor.arm("Peer")

    supervisor.forget_all()

    assert supervisor._tasks == {}
    assert supervisor._last_echo_at == {}
