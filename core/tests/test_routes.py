"""
connections/_routes.py -- `RouteTable`: who owns a (unit_code, opcode) route,
and the rule that exactly one thing does.

These run synchronously against the table itself. Before it was extracted, the
only way to reach this rule was a live socket pair plus a background thread --
which is why the interesting half of it (drop_unit, settle's identity check,
owner_of's fallback) had no coverage at all.
"""
import asyncio

import pytest

from core.connections._routes import RouteTable

ROUTE_A = (7, 1)
ROUTE_B = (7, 2)
#: Same opcode as ROUTE_A but a different unit -- what `drop_unit` has to
#: distinguish.
OTHER_UNIT_ROUTE = (8, 1)


@pytest.fixture
def loop():
    """A loop that is never run: `claim` only needs one to create futures on."""
    new_loop = asyncio.new_event_loop()
    try:
        yield new_loop
    finally:
        new_loop.close()


@pytest.fixture
def table():
    return RouteTable()


def noop(_message) -> None:
    ...


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #
def test_a_fresh_route_has_no_owner(table):
    assert table.owner_of(ROUTE_A) is None


def test_claiming_a_route_makes_its_future_the_owner(table, loop):
    future = table.claim(ROUTE_A, loop)
    assert table.owner_of(ROUTE_A) is future


def test_a_registered_callback_is_the_owner(table):
    table.register_callback(ROUTE_A, noop)
    assert table.owner_of(ROUTE_A) is noop


def test_a_done_future_stops_being_the_owner(table, loop):
    """The slot is only held while the receive is actually in flight."""
    future = table.claim(ROUTE_A, loop)
    future.cancel()
    assert table.owner_of(ROUTE_A) is None


# --------------------------------------------------------------------------- #
# The rule: a route is polled or handled, never both
# --------------------------------------------------------------------------- #
def test_a_second_claim_on_a_live_route_is_refused(table, loop):
    table.claim(ROUTE_A, loop)
    with pytest.raises(RuntimeError, match="only one receive_message"):
        table.claim(ROUTE_A, loop)


def test_claiming_over_a_callback_is_refused(table, loop):
    table.register_callback(ROUTE_A, noop)
    with pytest.raises(RuntimeError, match="already has an on-receive callback"):
        table.claim(ROUTE_A, loop)


def test_registering_a_callback_over_a_live_claim_is_refused(table, loop):
    table.claim(ROUTE_A, loop)
    with pytest.raises(RuntimeError, match="cannot be polled and handled at once"):
        table.register_callback(ROUTE_A, noop)


def test_a_second_callback_on_one_route_is_refused(table):
    table.register_callback(ROUTE_A, noop)
    with pytest.raises(RuntimeError, match="already has an on-receive callback"):
        table.register_callback(ROUTE_A, noop)


def test_a_settled_claim_frees_the_route_for_a_callback(table, loop):
    future = table.claim(ROUTE_A, loop)
    table.settle(ROUTE_A, future)
    table.register_callback(ROUTE_A, noop)  # must not raise
    assert table.owner_of(ROUTE_A) is noop


def test_routes_are_independent(table, loop):
    table.claim(ROUTE_A, loop)
    table.register_callback(ROUTE_B, noop)  # different route, no interference
    assert table.owner_of(ROUTE_B) is noop


# --------------------------------------------------------------------------- #
# settle / release
# --------------------------------------------------------------------------- #
def test_settle_removes_only_our_own_future(table, loop):
    """An abandoned call reaching `settle` late must not evict the subscriber
    that has since taken the slot -- that would strand them exactly as this
    whole rule exists to prevent."""
    first = table.claim(ROUTE_A, loop)
    table.settle(ROUTE_A, first)
    second = table.claim(ROUTE_A, loop)

    table.settle(ROUTE_A, first)  # the late arrival

    assert table.owner_of(ROUTE_A) is second


def test_release_frees_the_route_and_cancels_the_future(table, loop):
    future = table.claim(ROUTE_A, loop)
    table.release(ROUTE_A, future)
    assert future.cancelled()
    assert table.owner_of(ROUTE_A) is None


def test_release_leaves_an_already_settled_future_alone(table, loop):
    future = table.claim(ROUTE_A, loop)
    future.set_result("delivered")
    table.release(ROUTE_A, future)
    assert future.result() == "delivered"


# --------------------------------------------------------------------------- #
# Connect callbacks (keyed by unit, not route)
# --------------------------------------------------------------------------- #
def test_connect_callbacks_are_exclusive_per_unit(table):
    table.register_connect("Peer", noop)
    with pytest.raises(RuntimeError, match="already has an on-connect callback"):
        table.register_connect("Peer", noop)
    assert table.connect_callback("Peer") is noop


def test_unregister_connect_reports_whether_there_was_one(table):
    table.register_connect("Peer", noop)
    assert table.unregister_connect("Peer") is True
    assert table.unregister_connect("Peer") is False
    assert table.connect_callback("Peer") is None


def test_unregister_callback_reports_whether_there_was_one(table):
    table.register_callback(ROUTE_A, noop)
    assert table.unregister_callback(ROUTE_A) is True
    assert table.unregister_callback(ROUTE_A) is False


# --------------------------------------------------------------------------- #
# Per-unit teardown
# --------------------------------------------------------------------------- #
def test_drop_unit_fails_parked_receives_for_that_unit_only(table, loop):
    doomed = table.claim(ROUTE_A, loop)
    survivor = table.claim(OTHER_UNIT_ROUTE, loop)
    reason = ConnectionError("unit 'Peer' disconnected: echo timeout")

    table.drop_unit(7, reason)

    assert doomed.exception() is reason
    assert not survivor.done()
    assert table.owner_of(OTHER_UNIT_ROUTE) is survivor


def test_drop_unit_removes_callbacks_for_that_unit_only(table):
    table.register_callback(ROUTE_A, noop)
    table.register_callback(ROUTE_B, noop)
    table.register_callback(OTHER_UNIT_ROUTE, noop)

    table.drop_unit(7, ConnectionError("gone"))

    assert table.owner_of(ROUTE_A) is None
    assert table.owner_of(ROUTE_B) is None
    assert table.owner_of(OTHER_UNIT_ROUTE) is noop


def test_drop_unit_keeps_the_connect_callback(table):
    """A unit that drops may well come back, and should be greeted again."""
    table.register_connect("Peer", noop)
    table.drop_unit(7, ConnectionError("gone"))
    assert table.connect_callback("Peer") is noop


def test_drop_unit_with_an_unknown_code_is_a_no_op(table, loop):
    """`Connection` looks the code up and may get None for a unit it never had
    one for; nothing should match, and nothing should blow up."""
    future = table.claim(ROUTE_A, loop)
    table.drop_unit(None, ConnectionError("gone"))
    assert not future.done()


# --------------------------------------------------------------------------- #
# Connection teardown
# --------------------------------------------------------------------------- #
def test_drop_all_callbacks_clears_both_registries(table):
    table.register_callback(ROUTE_A, noop)
    table.register_connect("Peer", noop)

    table.drop_all_callbacks()

    assert table.owner_of(ROUTE_A) is None
    assert table.connect_callback("Peer") is None


def test_cancel_all_subscriptions_releases_every_parked_receive(table, loop):
    first = table.claim(ROUTE_A, loop)
    second = table.claim(OTHER_UNIT_ROUTE, loop)

    table.cancel_all_subscriptions()

    assert first.cancelled() and second.cancelled()
    assert table.owner_of(ROUTE_A) is None
    assert table.owner_of(OTHER_UNIT_ROUTE) is None


def test_cancel_all_subscriptions_leaves_a_delivered_future_alone(table, loop):
    future = table.claim(ROUTE_A, loop)
    future.set_result("delivered")
    table.cancel_all_subscriptions()
    assert future.result() == "delivered"
