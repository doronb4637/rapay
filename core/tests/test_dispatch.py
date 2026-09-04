"""
connections/base.py -- Connection's subscribe-or-drop dispatch, on-receive
callbacks, trigger_function, and echo consumption. Exercised over UDP (fast,
no handshake needed to get a peer address wired up).
"""
import socket
import threading
import time

import pytest

from core.connections.framing import pack_message

from core.tests._messages import (PING_OPCODE, PING_UNIT_CODE, TEXT_UNIT_CODE,
                                  TEXT_UNIT_CODE_2)


def _pair(manager, port, server_unit_code=100, client_unit_code=101,
          peer_unit_code=TEXT_UNIT_CODE, **extra):
    """A UDP server+client pair, both already `.start()`ed, talking as
    'Peer' on `port`. Returns (server, client).

    Both sides declare "Peer" under the SAME `peer_unit_code` -- IRS selects
    a message layout by that value regardless of physical direction (Text is
    one generic layout shared by both directions here), so both ends must
    agree on it. `server_unit_code`/`client_unit_code` are each side's own
    stamped-into-the-header identity and only need to differ from each
    other, not from `peer_unit_code`."""
    server = manager.create("server", {
        "protocol": "udp", "unitCode": server_unit_code, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": port, "unitCode": peer_unit_code}},
        **extra,
    })
    client = manager.create("client", {
        "protocol": "udp", "unitCode": client_unit_code, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": port, "unitCode": peer_unit_code}},
    })
    manager.start_all()
    return server, client


# --------------------------------------------------------------------------- #
# Subscribe-or-drop
# --------------------------------------------------------------------------- #
def test_message_sent_before_anyone_subscribes_is_dropped(manager, free_port):
    server, client = _pair(manager, free_port)
    client.send_message(b"nobody-home", 1)
    time.sleep(0.2)
    with pytest.raises(TimeoutError):
        server.receive_message(1, unit_name="Peer", timeout=0.3)


def test_receive_message_delivers_matching_opcode_and_unit(manager, free_port):
    server, client = _pair(manager, free_port)
    message = server.receive_message(
        1, unit_name="Peer", timeout=3,
        trigger_function=lambda: client.send_message(b"hello", 1),
    )
    assert bytes(message.data) == b"hello"


def _receive_expecting_timeout(connection, opcode, unit_name, timeout, result):
    """Runs receive_message() on a thread where the caller EXPECTS it to time
    out -- catches that one exception itself so pytest's thread-exception
    hook doesn't flag an intentional outcome as a warning."""
    try:
        result["value"] = connection.receive_message(opcode, unit_name, timeout=timeout)
    except TimeoutError:
        result["timed_out"] = True


def test_receive_message_ignores_a_different_opcode(manager, free_port):
    server, client = _pair(manager, free_port)
    result = {}
    t = threading.Thread(
        target=_receive_expecting_timeout, args=(server, 2, "Peer", 1, result), daemon=True
    )
    t.start()
    time.sleep(0.1)  # let the subscription arm
    client.send_message(b"wrong-opcode", 1)  # not opcode 2 -- dropped
    t.join(timeout=2)
    assert result.get("timed_out") is True  # still waiting / timed out, never delivered


def test_trigger_function_closes_the_request_response_race(manager, free_port):
    """trigger_function runs AFTER the subscription is armed but BEFORE the
    blocking wait -- so a reply that would otherwise beat a naive
    subscribe-then-send race is never dropped."""
    server, client = _pair(manager, free_port)
    # A UDP server only learns its peer's address from an inbound datagram
    # (`_remember_peer`) -- warm it up so `server.send_message` below has
    # somewhere to send, same as a real request/response exchange would.
    client.send_message(b"warm-up", 1)
    time.sleep(0.2)

    message = client.receive_message(
        2, unit_name="Peer", timeout=3,
        trigger_function=lambda: server.send_message(b"immediate", 2, unit_name="Peer"),
    )
    assert bytes(message.data) == b"immediate"


def test_trigger_function_raising_releases_the_subscription(manager, free_port):
    server, client = _pair(manager, free_port)

    def boom():
        raise RuntimeError("trigger blew up")

    with pytest.raises(RuntimeError, match="trigger blew up"):
        server.receive_message(1, unit_name="Peer", timeout=1, trigger_function=boom)

    # The route must be free again -- not left "subscribed" forever.
    message = server.receive_message(
        1, unit_name="Peer", timeout=3,
        trigger_function=lambda: client.send_message(b"after-failure", 1),
    )
    assert bytes(message.data) == b"after-failure"


def test_single_subscriber_per_route(manager, free_port):
    server, client = _pair(manager, free_port)
    result = {}
    t = threading.Thread(
        target=_receive_expecting_timeout, args=(server, 1, "Peer", 2, result), daemon=True
    )
    t.start()
    time.sleep(0.15)
    with pytest.raises(RuntimeError):
        server.receive_message(1, unit_name="Peer", timeout=0.5)
    t.join(timeout=3)


# --------------------------------------------------------------------------- #
# handle_on_receive -- standing callbacks
# --------------------------------------------------------------------------- #
def test_handle_on_receive_answers_every_matching_message(manager, free_port):
    server, client = _pair(manager, free_port)

    def echo_back(message):
        server.send_message(b"re:" + bytes(message.data), 2, unit_name="Peer")

    server.handle_on_receive(1, echo_back, unit_name="Peer")

    for i in range(3):
        reply = client.receive_message(
            2, unit_name="Peer", timeout=3,
            trigger_function=lambda i=i: client.send_message(f"n{i}".encode(), 1),
        )
        assert bytes(reply.data) == f"re:n{i}".encode()


def test_handle_on_receive_and_receive_message_are_mutually_exclusive(manager, free_port):
    server, client = _pair(manager, free_port)
    server.handle_on_receive(1, lambda message: None, unit_name="Peer")
    with pytest.raises(RuntimeError):
        server.receive_message(1, unit_name="Peer", timeout=0.3)


def test_receive_message_then_handle_on_receive_also_refused(manager, free_port):
    server, client = _pair(manager, free_port)
    result = {}
    t = threading.Thread(
        target=_receive_expecting_timeout, args=(server, 1, "Peer", 2, result), daemon=True
    )
    t.start()
    time.sleep(0.15)
    with pytest.raises(RuntimeError):
        server.handle_on_receive(1, lambda message: None, unit_name="Peer")
    t.join(timeout=3)


def test_registering_a_second_callback_on_the_same_route_is_refused(manager, free_port):
    server, client = _pair(manager, free_port)
    server.handle_on_receive(1, lambda message: None, unit_name="Peer")
    with pytest.raises(RuntimeError):
        server.handle_on_receive(1, lambda message: None, unit_name="Peer")


def test_stop_on_receive_removes_the_callback(manager, free_port):
    server, client = _pair(manager, free_port)
    server.handle_on_receive(1, lambda message: None, unit_name="Peer")
    assert server.stop_on_receive(1, unit_name="Peer") is True
    assert server.stop_on_receive(1, unit_name="Peer") is False  # already gone

    # Route is free again: no callback firing, so a fresh send is dropped
    # (nobody subscribed either).
    client.send_message(b"post-stop", 1)
    time.sleep(0.2)
    with pytest.raises(TimeoutError):
        server.receive_message(1, unit_name="Peer", timeout=0.3)


def test_callback_exception_is_swallowed_and_does_not_kill_the_read_loop(manager, free_port, caplog):
    server, client = _pair(manager, free_port)

    def bad_callback(message):
        raise RuntimeError("boom in callback")

    server.handle_on_receive(1, bad_callback, unit_name="Peer")
    client.send_message(b"first", 1)
    time.sleep(0.3)

    # The connection is still alive and this route still works for a second
    # message -- a raising callback must not have killed anything.
    server.stop_on_receive(1, unit_name="Peer")

    def good_callback(message):
        server.send_message(b"still-alive", 2, unit_name="Peer")

    server.handle_on_receive(1, good_callback, unit_name="Peer")
    reply = client.receive_message(
        2, unit_name="Peer", timeout=3,
        trigger_function=lambda: client.send_message(b"second", 1),
    )
    assert bytes(reply.data) == b"still-alive"


# --------------------------------------------------------------------------- #
# IRS decode failure on the wire
# --------------------------------------------------------------------------- #
def test_malformed_payload_is_dropped_and_the_receive_stays_parked(manager, free_port):
    """A payload IRS can't parse into the registered layout costs exactly that
    message: it is logged and dropped, and the caller waiting on that route goes
    on waiting for one it can actually return. A peer sending a bad frame must
    not be able to fail somebody who asked for a good one."""
    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        # PING_UNIT_CODE: opcode 21 is registered as Ping, whose body is 5 bytes.
        "connections": {"Peer": {"port": free_port, "unitCode": PING_UNIT_CODE}},
    })
    sender = manager.create("sender", {
        "protocol": "udp", "unitCode": PING_UNIT_CODE, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Server": {"port": free_port, "unitCode": PING_UNIT_CODE}},
    })
    manager.start_all()

    t_result = {}
    def _run():
        try:
            t_result["value"] = server.receive_message(PING_OPCODE, unit_name="Peer", timeout=5)
        except BaseException as exc:
            t_result["exc"] = exc
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(0.15)

    # A raw malformed datagram -- too short for Ping's 5-byte body -- built by
    # hand so it bypasses the framework's own (correct) encoder.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(pack_message(PING_UNIT_CODE, PING_OPCODE, b"\x01"), ("127.0.0.1", free_port))
    sock.close()
    time.sleep(0.3)
    assert t.is_alive(), f"the bad message ended the receive instead of being dropped: {t_result}"

    # The route still belongs to that caller, so the next VALID message on it
    # is what returns.
    sender.send_message({"seq": 7, "kind": 1, "value": 9}, PING_OPCODE)
    t.join(timeout=3)
    assert "exc" not in t_result, t_result
    message = t_result["value"]
    assert (message.seq, message.value) == (7, 9)


# --------------------------------------------------------------------------- #
# Echo consumption -- never visible to the application
# --------------------------------------------------------------------------- #
def test_echo_opcode_is_consumed_and_never_delivered(manager, free_port):
    # opcode 15 must itself be IRS-registered for TEXT_UNIT_CODE: receive_message
    # validates the route eagerly, before dispatch ever gets a chance to
    # decide "this is an echo" -- so this proves consumption, not a routing gap.
    server, client = _pair(
        manager, free_port,
        echo_opcode=15, EchoInterval=0.2, EchoTimeout=30,
    )
    client.send_message(b"heartbeat", 15)
    time.sleep(0.2)
    with pytest.raises(TimeoutError):
        server.receive_message(15, unit_name="Peer", timeout=0.3)


def test_non_echo_opcode_still_delivered_on_a_connection_with_echo_enabled(manager, free_port):
    server, client = _pair(
        manager, free_port,
        echo_opcode=15, EchoInterval=0.2, EchoTimeout=30,
    )
    message = server.receive_message(
        1, unit_name="Peer", timeout=3,
        trigger_function=lambda: client.send_message(b"app-data", 1),
    )
    assert bytes(message.data) == b"app-data"


# --------------------------------------------------------------------------- #
# Per-unit teardown: _disconnect_unit (the echo watchdog's hammer)
# --------------------------------------------------------------------------- #
def _two_unit_server(manager, ports):
    """A UDP server multiplexing two units, so a per-unit teardown can be
    shown to leave the other one alone."""
    return manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {
            "PeerA": {"port": ports[0], "unitCode": TEXT_UNIT_CODE},
            "PeerB": {"port": ports[1], "unitCode": TEXT_UNIT_CODE_2},
        },
    })


def _disconnect(connection, unit_name):
    """Drive `_disconnect_unit` directly. In production only the echo watchdog
    calls it, which costs a real EchoTimeout to provoke; the cleanup it
    performs is the same either way."""
    connection._loop_thread.await_coroutine(connection._disconnect_unit(unit_name))


def test_disconnect_unit_fails_a_parked_receive_message(manager, free_ports):
    server = _two_unit_server(manager, free_ports(2))
    server.start()

    result = {}
    def _run():
        try:
            result["value"] = server.receive_message(1, unit_name="PeerA", timeout=5)
        except BaseException as exc:
            result["exc"] = exc
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    time.sleep(0.15)

    _disconnect(server, "PeerA")

    thread.join(timeout=3)
    assert isinstance(result.get("exc"), ConnectionError), result


def test_disconnect_unit_drops_only_that_units_callbacks(manager, free_ports):
    server = _two_unit_server(manager, free_ports(2))
    server.start()
    server.handle_on_receive(1, lambda message: None, unit_name="PeerA")
    server.handle_on_receive(1, lambda message: None, unit_name="PeerB")

    _disconnect(server, "PeerA")

    assert server.stop_on_receive(1, unit_name="PeerA") is False  # already dropped
    assert server.stop_on_receive(1, unit_name="PeerB") is True   # untouched


def test_disconnect_unit_cancels_only_that_units_periodic_senders(manager, free_ports):
    server = _two_unit_server(manager, free_ports(2))
    server.start()
    server.periodic_sending(b"tick", 1, 0.05, unit_name="PeerA")
    server.periodic_sending(b"tick", 1, 0.05, unit_name="PeerB")

    _disconnect(server, "PeerA")

    assert server.stop_periodic(1, unit_name="PeerA") is False  # already cancelled
    assert server.stop_periodic(1, unit_name="PeerB") is True   # untouched


def test_disconnect_unit_leaves_the_unit_inactive(manager, free_ports):
    ports = free_ports(2)
    server = _two_unit_server(manager, ports)
    server.start()

    # A UDP server learns a peer from its first inbound datagram.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(pack_message(TEXT_UNIT_CODE, 1, b"hello"), ("127.0.0.1", ports[0]))
    sock.sendto(pack_message(TEXT_UNIT_CODE_2, 1, b"hello"), ("127.0.0.1", ports[1]))
    sock.close()
    assert server.wait_for_connected_units(["PeerA", "PeerB"], timeout=3) is True

    _disconnect(server, "PeerA")

    assert "PeerA" not in server.active_units
    assert "PeerB" in server.active_units
