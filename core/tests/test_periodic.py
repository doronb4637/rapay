"""
connections/base.py -- `periodic_sending` / `stop_periodic`: the repeating
counterpart to `send_message`, filed under the same (unit_code, opcode) route
key as a subscription.

Characterization coverage: these behaviours had none, and the route bookkeeping
they rely on is shared with the subscription machinery.
"""
import threading
import time

import pytest

from core.tests._messages import TEXT_UNIT_CODE

#: Long enough that a sender left running by mistake fires several more times
#: within a test's observation window; short enough to keep the suite quick.
FAST_INTERVAL = 0.05
#: Effectively "never again" -- a replaced sender emits its first message
#: immediately and then goes quiet for the rest of the test.
SLOW_INTERVAL = 30.0


class Counter:
    """Counts on-receive callbacks from the executor threads they run on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0

    def __call__(self, _message) -> None:
        with self._lock:
            self.count += 1

    def read(self) -> int:
        with self._lock:
            return self.count


def _pair(manager, port):
    """A started UDP server+client pair talking as 'Peer' on `port`."""
    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": port, "unitCode": TEXT_UNIT_CODE}},
    })
    client = manager.create("client", {
        "protocol": "udp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": port, "unitCode": TEXT_UNIT_CODE}},
    })
    manager.start_all()
    return server, client


def test_periodic_sending_repeats(manager, free_port):
    server, client = _pair(manager, free_port)
    counter = Counter()
    server.handle_on_receive(1, counter, unit_name="Peer")

    client.periodic_sending(b"tick", 1, FAST_INTERVAL)
    time.sleep(FAST_INTERVAL * 8)

    assert counter.read() >= 3, "periodic sender did not repeat"


def test_periodic_sending_replaces_the_sender_for_a_route(manager, free_port):
    """Calling it twice for one route must cancel the first sender, not run
    both -- two overlapping senders would silently double the send rate."""
    server, client = _pair(manager, free_port)
    counter = Counter()
    server.handle_on_receive(1, counter, unit_name="Peer")

    client.periodic_sending(b"fast", 1, FAST_INTERVAL)
    time.sleep(FAST_INTERVAL * 6)
    assert counter.read() >= 2

    client.periodic_sending(b"slow", 1, SLOW_INTERVAL)
    after_replace = counter.read()
    time.sleep(FAST_INTERVAL * 10)

    # The replacement sends once immediately, then sleeps out the test. Had the
    # fast sender survived, this window alone would carry ~10 more messages.
    assert counter.read() - after_replace <= 2


def test_stop_periodic_reports_whether_there_was_one_to_stop(manager, free_port):
    _server, client = _pair(manager, free_port)
    client.periodic_sending(b"tick", 1, FAST_INTERVAL)

    assert client.stop_periodic(1, unit_name="Peer") is True
    assert client.stop_periodic(1, unit_name="Peer") is False


def test_stopped_sender_actually_stops(manager, free_port):
    server, client = _pair(manager, free_port)
    counter = Counter()
    server.handle_on_receive(1, counter, unit_name="Peer")

    client.periodic_sending(b"tick", 1, FAST_INTERVAL)
    time.sleep(FAST_INTERVAL * 6)
    client.stop_periodic(1, unit_name="Peer")
    settled = counter.read()

    time.sleep(FAST_INTERVAL * 8)
    assert counter.read() == settled


def test_periodic_sending_rejects_a_non_positive_interval(manager, free_port):
    _server, client = _pair(manager, free_port)
    for bad in (0, -1, -0.5):
        with pytest.raises(ValueError, match="interval must be > 0"):
            client.periodic_sending(b"tick", 1, bad)


def test_close_cancels_periodic_senders(manager, free_port):
    server, client = _pair(manager, free_port)
    counter = Counter()
    server.handle_on_receive(1, counter, unit_name="Peer")

    client.periodic_sending(b"tick", 1, FAST_INTERVAL)
    time.sleep(FAST_INTERVAL * 6)
    client.close()
    settled = counter.read()

    time.sleep(FAST_INTERVAL * 8)
    assert counter.read() == settled


def test_periodic_sending_and_a_standing_callback_share_the_route_key(manager, free_port):
    """A periodic sender and an on-receive callback are filed under the same
    (unit_code, opcode) key in different tables, so registering one must not
    disturb the other."""
    server, client = _pair(manager, free_port)
    counter = Counter()
    client.handle_on_receive(1, counter, unit_name="Peer")

    client.periodic_sending(b"tick", 1, FAST_INTERVAL)
    assert client.stop_periodic(1, unit_name="Peer") is True
    assert client.stop_on_receive(1, unit_name="Peer") is True
