"""
Functional smoke test / usage demo for connection_framework.

Note on test structure: the new subscribe-or-drop delivery model means a
receiver must already be "subscribed" (i.e. its receive_message() call must
already be in flight, registered as a waiting future) before the sender
fires -- otherwise the message is dropped, per spec, and never delivered.
So these tests run receive_message() on a background thread and give it a
brief head start before sending, the way two independent processes
naturally would (one polling, one firing whenever it's ready).

Run directly: python3 test_framework.py
"""
import asyncio
import sys
import threading
import time

sys.path.insert(0, ".")

from connections.manager import ConnectionManager


def _receive_in_background(connection, opcode, unit_name, timeout, results, key):
    """Runs receive_message() on a background thread and stores the result
    (or the exception, as a sentinel) into results[key]."""
    def _run():
        try:
            results[key] = connection.receive_message(opcode, unit_name=unit_name, timeout=timeout)
        except asyncio.TimeoutError:
            results[key] = asyncio.TimeoutError
        except Exception as exc:  # e.g. ConnectionError on echo-timeout disconnect
            results[key] = exc
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(0.2)  # let the subscription actually register before we send
    return t


def test_tcp_roundtrip():
    print("\n=== TCP round trip (multi-unit, mandatory opcode) ===")
    RADAR_OPCODE, TRACKER_OPCODE, ACK_OPCODE = 1, 2, 3
    mgr = ConnectionManager()

    server_cfg = {
        "protocol": "tcp",
        "side": "server",
        "ip": "127.0.0.1",
        "local_ip": "127.0.0.1",
        "ports": [15000, 15001], "units": {"15000": "RadarUnit", "15001": "TrackerUnit"},
    }
    client_cfg = {
        "protocol": "tcp",
        "side": "client",
        "ip": "127.0.0.1",
        "local_ip": "127.0.0.1",
        "ports": [15000, 15001], "units": {"15000": "RadarUnit", "15001": "TrackerUnit"},
    }
    server = mgr.create("tcp_server", server_cfg)
    client = mgr.create("tcp_client", client_cfg)
    server.start()
    client.start()
    time.sleep(0.2)

    results = {}
    t1 = _receive_in_background(server, RADAR_OPCODE, "RadarUnit", 3, results, "radar")
    client.send_message(b"hello-radar", RADAR_OPCODE, unit_name="RadarUnit")
    t1.join(timeout=4)
    assert results["radar"] == ("RadarUnit", b"hello-radar"), results
    print(f"server received (opcode={RADAR_OPCODE}): {results['radar']}")

    results2 = {}
    t2 = _receive_in_background(server, TRACKER_OPCODE, "TrackerUnit", 3, results2, "tracker")
    client.send_message(b"hello-tracker", TRACKER_OPCODE, unit_name="TrackerUnit")
    t2.join(timeout=4)
    assert results2["tracker"] == ("TrackerUnit", b"hello-tracker"), results2
    print(f"server received (opcode={TRACKER_OPCODE}): {results2['tracker']}")

    # Reply path, server -> client, still requires unit_name (multi-unit connection)
    results3 = {}
    t3 = _receive_in_background(client, ACK_OPCODE, "RadarUnit", 3, results3, "ack")
    server.send_message(b"ack-radar", ACK_OPCODE, unit_name="RadarUnit")
    t3.join(timeout=4)
    assert results3["ack"] == ("RadarUnit", b"ack-radar"), results3
    print("TCP round trip OK (framing + multi-unit + opcode routing verified)")

    mgr.shutdown_all()
    print("TCP connections fully torn down")


def test_udp_single_unit():
    print("\n=== UDP round trip (single unit, explicit unit_map) ===")
    PING_OPCODE, PONG_OPCODE = 10, 11
    mgr = ConnectionManager()

    server_cfg = {
        "protocol": "udp", "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [16000], "units": {"16000": "PingClient"},
    }
    client_cfg = {
        "protocol": "udp", "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [16000], "units": {"16000": "PingServer"},
    }
    server = mgr.create("udp_server", server_cfg)
    client = mgr.create("udp_client", client_cfg)
    server.start()
    client.start()

    # Single connected unit -> unit_name is still OPTIONAL; opcode is NOT.
    results = {}
    t1 = _receive_in_background(server, PING_OPCODE, None, 3, results, "ping")
    client.send_message(b"ping", PING_OPCODE)
    t1.join(timeout=4)
    unit, payload = results["ping"]
    print(f"server received on {unit!r} (opcode={PING_OPCODE}): {payload!r}")
    assert unit == "PingClient" and payload == b"ping"

    results2 = {}
    t2 = _receive_in_background(client, PONG_OPCODE, None, 3, results2, "pong")
    server.send_message(b"pong", PONG_OPCODE, unit_name=unit)
    t2.join(timeout=4)
    assert results2["pong"][1] == b"pong"
    print("UDP round trip OK")

    mgr.shutdown_all()
    print("UDP connections fully torn down")


def test_composite_unit():
    print("\n=== Composite Unit challenge: send-only + receive-only combined ===")
    print("(Using two directional UDP links: this sandbox's network namespace")
    print(" has no multicast routing. multicast.py now derives direction from")
    print(" config.side (SENDER/RECEIVER) and plugs into CompositeUnit exactly")
    print(" the same way as the UDP links shown here.)")
    BEACON_OPCODE, ACK_OPCODE = 20, 21
    mgr = ConnectionManager()
    outbound_cfg = {
        "protocol": "udp",
        "side": "client",
        "ip": "127.0.0.1",
        "local_ip": "127.0.0.1",
        "ports": 17100,
        "units": {17100: "BeaconUnit"},
        "mode": "send_only",
    }
    inbound_cfg = {
        "protocol": "udp",
        "side": "server",
        "ip": "127.0.0.1",
        "local_ip": "127.0.0.1",
        "ports": [17101],
        "units": {17101: "BeaconUnit"},
        "mode": "receive_only",
    }
    beacon = mgr.create_composite("BeaconUnit", {"transport": outbound_cfg, "receive": inbound_cfg})

    peer_listen_cfg = {
        "protocol": "udp", "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [17100], "units": {"17100": "BeaconUnit"}, "mode": "receive_only",
    }
    peer_reply_cfg = {
        "protocol": "udp", "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [17101], "units": {"17101": "BeaconUnit"},
    }
    peer_listener = mgr.create("peer_listener", peer_listen_cfg)
    peer_replier = mgr.create("peer_replier", peer_reply_cfg)

    mgr.start_all()

    results = {}
    t1 = _receive_in_background(peer_listener, BEACON_OPCODE, "BeaconUnit", 3, results, "beacon")
    beacon.send_message(b"beacon-out", BEACON_OPCODE)
    t1.join(timeout=4)
    assert results["beacon"][1] == b"beacon-out"
    print(f"peer received: {results['beacon'][1]!r}")

    results2 = {}
    t2 = _receive_in_background(beacon, ACK_OPCODE, "BeaconUnit", 3, results2, "ack")
    peer_replier.send_message(b"beacon-ack", ACK_OPCODE, unit_name="BeaconUnit")
    t2.join(timeout=4)
    assert results2["ack"][1] == b"beacon-ack"
    print(f"BeaconUnit received via its receive-only UDP member: {results2['ack'][1]!r}")

    try:
        beacon._receiver.send_message(b"should-fail", 0, unit_name="BeaconUnit")
        raise AssertionError("receive-only member should have refused to send")
    except RuntimeError as exc:
        print(f"receive-only member correctly refused to send: {exc}")

    mgr.shutdown_all()
    print("Composite unit fully torn down (both members closed)")


def test_message_filtering():
    print("\n=== Message filtering: unsubscribed messages are dropped, not queued ===")
    UNSUBSCRIBED_OPCODE = 77
    mgr = ConnectionManager()

    server_cfg = {
        "protocol": "udp", "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [18000], "units": {"18000": "FilterUnit"},
    }
    client_cfg = {
        "protocol": "udp", "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [18000], "units": {"18000": "FilterUnit"},
    }
    server = mgr.create("filter_server", server_cfg)
    client = mgr.create("filter_client", client_cfg)
    server.start()
    client.start()
    time.sleep(0.1)

    # Nobody is subscribed (no receive_message() call in flight) when this
    # arrives -- it must be silently dropped, not buffered for later.
    client.send_message(b"nobody-is-listening", UNSUBSCRIBED_OPCODE)
    time.sleep(0.2)

    try:
        server.receive_message(UNSUBSCRIBED_OPCODE, timeout=0.5)
        raise AssertionError("expected the earlier, unsubscribed message to have been dropped")
    except asyncio.TimeoutError:
        print("confirmed: message sent before anyone subscribed was correctly dropped")

    mgr.shutdown_all()
    print("Message filtering connections fully torn down")


def test_automatic_echo():
    print("\n=== Automatic echo handling (distinct request/reply opcodes) ===")
    ECHO_REQUEST_OPCODE, ECHO_REPLY_OPCODE = 99, 100
    mgr = ConnectionManager()

    server_cfg = {
        "protocol": "udp", "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [19000], "units": {"19000": "EchoUnit"},
        "recv_echo_opcode": ECHO_REQUEST_OPCODE, "send_echo_opcode": ECHO_REPLY_OPCODE,
        # Long interval/timeout: this test is about the auto-REPLY path, so
        # keep the periodic sender out of the way.
        "EchoInterval": 30, "EchoTimeout": 60,
    }
    client_cfg = {
        "protocol": "udp", "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [19000], "units": {"19000": "EchoUnit"},
    }
    server = mgr.create("echo_server", server_cfg)
    client = mgr.create("echo_client", client_cfg)
    server.start()
    client.start()
    time.sleep(0.1)

    # Client subscribes to the echo REPLY opcode before sending the request.
    results = {}
    t1 = _receive_in_background(client, ECHO_REPLY_OPCODE, "EchoUnit", 3, results, "echo")
    client.send_message(b"ping-me", ECHO_REQUEST_OPCODE)
    t1.join(timeout=4)
    assert results["echo"] == ("EchoUnit", b"ping-me")
    print(f"client automatically received echo reply: {results['echo'][1]!r}")

    # The original request must NEVER be visible via receive_message() on
    # the server -- automatic echo handling intercepts it before
    # subscribe-or-drop even runs, regardless of whether anything would
    # have been subscribed to it.
    try:
        server.receive_message(ECHO_REQUEST_OPCODE, timeout=0.5)
        raise AssertionError("echo request should have been intercepted, not delivered")
    except asyncio.TimeoutError:
        print("confirmed: echo request was handled automatically and never reached the app")

    mgr.shutdown_all()
    print("Echo-handling connections fully torn down")


def test_single_opcode_heartbeat():
    print("\n=== Periodic echo: one shared echo_opcode, no echo storm ===")
    HEARTBEAT_OPCODE = 55
    mgr = ConnectionManager()

    common = {
        "protocol": "udp", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "echo_opcode": HEARTBEAT_OPCODE, "EchoInterval": 0.3, "EchoTimeout": 5.0,
    }
    peer_a = mgr.create("hb_a", {**common, "side": "server", "ports": [21000],
                                 "units": {"21000": "PeerB"}})
    peer_b = mgr.create("hb_b", {**common, "side": "client", "ports": [21000],
                                 "units": {"21000": "PeerA"}})

    # Count actual sends per peer. With a shared opcode, auto-replying on
    # receipt would make each peer answer the other's answer forever; the
    # send count is what makes that visible rather than merely suspected.
    sent = {"a": 0, "b": 0}

    def _count_sends(connection, slot):
        original = connection._do_send

        async def counted(unit_name, data, opcode):
            sent[slot] += 1
            return await original(unit_name, data, opcode)

        connection._do_send = counted

    _count_sends(peer_a, "a")
    _count_sends(peer_b, "b")

    mgr.start_all()
    time.sleep(1.6)  # ~5 heartbeat intervals

    # Both peers must still consider each other alive (watchdog never fired).
    assert "PeerB" in peer_a._last_echo_at, "peer A lost its liveness entry"
    assert "PeerA" in peer_b._last_echo_at, "peer B lost its liveness entry"
    assert peer_a._transports.get("PeerB") is not None, "peer A was disconnected"
    assert peer_b._transports.get("PeerA") is not None, "peer B was disconnected"

    # ~5 scheduled heartbeats each; a storm would be orders of magnitude more.
    assert sent["a"] < 20 and sent["b"] < 20, f"echo storm detected: {sent}"
    print(f"heartbeats stayed on schedule (sends: {sent}), both links alive")

    mgr.shutdown_all()
    print("Heartbeat connections fully torn down")


def test_echo_timeout_disconnect():
    print("\n=== EchoTimeout: a silent unit is disconnected automatically ===")
    HEARTBEAT_OPCODE = 56
    mgr = ConnectionManager()

    # Points at a port where nothing is listening, so no echo ever comes back.
    lonely = mgr.create("lonely", {
        "protocol": "udp", "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [21500], "units": {"21500": "GhostUnit"},
        "echo_opcode": HEARTBEAT_OPCODE, "EchoInterval": 0.2, "EchoTimeout": 0.6,
    })
    lonely.start()
    assert lonely._transports.get("GhostUnit") is not None

    time.sleep(1.5)  # well past EchoTimeout

    assert lonely._transports.get("GhostUnit") is None, "unit should have been disconnected"
    try:
        lonely.send_message(b"anyone-there", 1)
        raise AssertionError("expected sending on the disconnected unit to fail")
    except ConnectionError as exc:
        print(f"confirmed: unit dropped after EchoTimeout, sends now refused ({exc})")

    mgr.shutdown_all()
    print("Echo-timeout connection fully torn down")


def test_periodic_sending():
    print("\n=== periodic_sending / stop_periodic ===")
    TICK_OPCODE = 60
    mgr = ConnectionManager()

    server = mgr.create("tick_server", {
        "protocol": "udp", "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [22000], "units": {"22000": "TickClient"},
    })
    client = mgr.create("tick_client", {
        "protocol": "udp", "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [22000], "units": {"22000": "TickServer"},
    })
    mgr.start_all()

    # unit_name omitted: both sides have exactly one connected unit.
    client.periodic_sending(TICK_OPCODE, b"tick", 0.2)
    for i in range(3):
        unit, payload = server.receive_message(TICK_OPCODE, timeout=2)
        assert payload == b"tick", payload
    print("received 3 consecutive ticks from one periodic_sending() call")

    # Task replacement: same (unitCode, opcode) route -> old sender cancelled.
    client.periodic_sending(TICK_OPCODE, b"tock", 0.2)
    time.sleep(0.5)  # let any already-queued "tick" land and be dropped
    for i in range(2):
        unit, payload = server.receive_message(TICK_OPCODE, timeout=2)
        assert payload == b"tock", f"old periodic task was not replaced: {payload!r}"
    print("re-calling periodic_sending() replaced the old task rather than stacking")

    assert client.stop_periodic(TICK_OPCODE) is True
    assert client.stop_periodic(TICK_OPCODE) is False, "second stop should report nothing running"
    time.sleep(0.4)  # let anything already in flight arrive and be dropped
    try:
        server.receive_message(TICK_OPCODE, timeout=1.0)
        raise AssertionError("expected no further ticks after stop_periodic()")
    except asyncio.TimeoutError:
        print("confirmed: stop_periodic() halted the background sender")

    mgr.shutdown_all()
    print("Periodic-sending connections fully torn down")


def test_single_subscription_per_route():
    print("\n=== One subscriber per (unitCode, opcode) route ===")
    BUSY_OPCODE = 70
    mgr = ConnectionManager()

    server = mgr.create("busy_server", {
        "protocol": "udp", "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "ports": [23000], "units": {"23000": "BusyUnit"},
    })
    server.start()

    results = {}
    t1 = _receive_in_background(server, BUSY_OPCODE, "BusyUnit", 2, results, "first")
    try:
        server.receive_message(BUSY_OPCODE, unit_name="BusyUnit", timeout=1)
        raise AssertionError("a second subscriber for the same route should be rejected")
    except RuntimeError as exc:
        print(f"confirmed: duplicate subscription refused ({exc})")
    t1.join(timeout=4)

    mgr.shutdown_all()
    print("Subscription connections fully torn down")


if __name__ == "__main__":
    test_tcp_roundtrip()
    test_udp_single_unit()
    test_composite_unit()
    test_message_filtering()
    test_automatic_echo()
    test_single_opcode_heartbeat()
    test_echo_timeout_disconnect()
    test_periodic_sending()
    test_single_subscription_per_route()
    print("\nALL TESTS PASSED")
