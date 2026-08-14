"""
connections/base.py -- Connection's subscribe-or-drop dispatch, on-receive
callbacks, trigger_function, and echo consumption. Exercised over UDP (fast,
no handshake needed to get a peer address wired up).
"""
import threading
import time

import pytest

from tests._messages import TEXT_UNIT_CODE, TEXT_UNIT_CODE_2


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
        server.receive_message(1, unitName="Peer", timeout=0.3)


def test_receive_message_delivers_matching_opcode_and_unit(manager, free_port):
    server, client = _pair(manager, free_port)
    unit, message = server.receive_message(
        1, unitName="Peer", timeout=3,
        trigger_function=lambda: client.send_message(b"hello", 1),
    )
    assert unit == "Peer"
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

    unit, message = client.receive_message(
        2, unitName="Peer", timeout=3,
        trigger_function=lambda: server.send_message(b"immediate", 2, unit_name="Peer"),
    )
    assert bytes(message.data) == b"immediate"


def test_trigger_function_raising_releases_the_subscription(manager, free_port):
    server, client = _pair(manager, free_port)

    def boom():
        raise RuntimeError("trigger blew up")

    with pytest.raises(RuntimeError, match="trigger blew up"):
        server.receive_message(1, unitName="Peer", timeout=1, trigger_function=boom)

    # The route must be free again -- not left "subscribed" forever.
    unit, message = server.receive_message(
        1, unitName="Peer", timeout=3,
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
        server.receive_message(1, unitName="Peer", timeout=0.5)
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
        unit, reply = client.receive_message(
            2, unitName="Peer", timeout=3,
            trigger_function=lambda i=i: client.send_message(f"n{i}".encode(), 1),
        )
        assert bytes(reply.data) == f"re:n{i}".encode()


def test_handle_on_receive_and_receive_message_are_mutually_exclusive(manager, free_port):
    server, client = _pair(manager, free_port)
    server.handle_on_receive(1, lambda message: None, unit_name="Peer")
    with pytest.raises(RuntimeError):
        server.receive_message(1, unitName="Peer", timeout=0.3)


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
        server.receive_message(1, unitName="Peer", timeout=0.3)


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
    unit, reply = client.receive_message(
        2, unitName="Peer", timeout=3,
        trigger_function=lambda: client.send_message(b"second", 1),
    )
    assert bytes(reply.data) == b"still-alive"


# --------------------------------------------------------------------------- #
# IRS decode failure on the wire
# --------------------------------------------------------------------------- #
def test_malformed_payload_fails_the_parked_receive_message(manager, free_port):
    """A payload IRS can't parse into the registered layout surfaces as an
    exception to whoever is waiting -- it must not hang until timeout."""
    from connections.framing import pack_message

    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": 210}},  # PING_UNIT_CODE, opcode 21 needs 5 bytes
    })
    manager.start_all()

    t_result = {}
    def _run():
        try:
            t_result["value"] = server.receive_message(21, unitName="Peer", timeout=2)
        except BaseException as exc:
            t_result["exc"] = exc
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(0.15)

    # Send a raw malformed datagram directly -- too short for Ping's 5-byte
    # body -- bypassing the framework's own (correct) encoder.
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(pack_message(210, 21, b"\x01"), ("127.0.0.1", free_port))
    sock.close()

    t.join(timeout=3)
    assert "exc" in t_result, t_result


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
        server.receive_message(15, unitName="Peer", timeout=0.3)


def test_non_echo_opcode_still_delivered_on_a_connection_with_echo_enabled(manager, free_port):
    server, client = _pair(
        manager, free_port,
        echo_opcode=15, EchoInterval=0.2, EchoTimeout=30,
    )
    unit, message = server.receive_message(
        1, unitName="Peer", timeout=3,
        trigger_function=lambda: client.send_message(b"app-data", 1),
    )
    assert bytes(message.data) == b"app-data"
