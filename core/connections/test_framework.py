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
import logging
import sys
import threading
import time
from pathlib import Path

# Cwd-independent: `core` is an ordinary package rooted at the REPO ROOT
# (`core.connections`, `core.IRS`, `core.tools`, `core.annotations`), so that
# root -- this file's great-grandparent -- is what has to go on `sys.path`,
# not "." (only right if the caller's cwd already happens to be the repo root)
# and not `core/` itself (there is no top-level `connections`/`IRS`/`tools`
# package any more for that to resolve).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.connections.config import ConnectionConfig
from core.connections.framing import unpack_header
from core.connections.handlers import UnitHandler, route
from core.connections.manager import ConnectionManager

# receive_message() refuses routes IRS doesn't know, so the layouts have to be
# registered before any test subscribes.
import core.IRS.Structures.Test.test_messages  # noqa: F401


def _received(result):
    """(unit, Text) -> (unit, bytes), for the tests that only assert bytes moved."""
    unit, message = result
    return unit, bytes(message.data)


def _receive_in_background(connection, opcode, unit_name, timeout, results, key):
    """Runs receive_message() on a background thread and stores the result
    (or the exception, as a sentinel) into results[key]."""
    def _run():
        try:
            # Positional: CompositeUnit still spells this parameter unit_name.
            results[key] = connection.receive_message(opcode, unit_name, timeout=timeout)
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
        "unitCode": 100,
        "side": "server",
        "ip": "127.0.0.1",
        "local_ip": "127.0.0.1",
        "connections": {"RadarUnit": {"port": 15000, "unitCode": 7},
                        "TrackerUnit": {"port": 15001, "unitCode": 8}},
    }
    client_cfg = {
        "protocol": "tcp",
        "unitCode": 101,
        "side": "client",
        "ip": "127.0.0.1",
        "local_ip": "127.0.0.1",
        "connections": {"RadarUnit": {"port": 15000, "unitCode": 7},
                        "TrackerUnit": {"port": 15001, "unitCode": 8}},
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
    assert _received(results["radar"]) == ("RadarUnit", b"hello-radar"), results
    print(f"server received (opcode={RADAR_OPCODE}): {_received(results['radar'])}")

    results2 = {}
    t2 = _receive_in_background(server, TRACKER_OPCODE, "TrackerUnit", 3, results2, "tracker")
    client.send_message(b"hello-tracker", TRACKER_OPCODE, unit_name="TrackerUnit")
    t2.join(timeout=4)
    assert _received(results2["tracker"]) == ("TrackerUnit", b"hello-tracker"), results2
    print(f"server received (opcode={TRACKER_OPCODE}): {_received(results2['tracker'])}")

    # Reply path, server -> client, still requires unit_name (multi-unit connection)
    results3 = {}
    t3 = _receive_in_background(client, ACK_OPCODE, "RadarUnit", 3, results3, "ack")
    server.send_message(b"ack-radar", ACK_OPCODE, unit_name="RadarUnit")
    t3.join(timeout=4)
    assert _received(results3["ack"]) == ("RadarUnit", b"ack-radar"), results3
    print("TCP round trip OK (framing + multi-unit + opcode routing verified)")

    mgr.shutdown_all()
    print("TCP connections fully torn down")


def test_udp_single_unit():
    print("\n=== UDP round trip (single unit, explicit connections block) ===")
    PING_OPCODE, PONG_OPCODE = 10, 11
    mgr = ConnectionManager()

    server_cfg = {
        "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"PingClient": {"port": 16000, "unitCode": 1}},
    }
    client_cfg = {
        "protocol": "udp", "unitCode": 111, "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"PingServer": {"port": 16000, "unitCode": 1}},
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
    unit, payload = _received(results["ping"])
    print(f"server received on {unit!r} (opcode={PING_OPCODE}): {payload!r}")
    assert unit == "PingClient" and payload == b"ping"

    results2 = {}
    t2 = _receive_in_background(client, PONG_OPCODE, None, 3, results2, "pong")
    server.send_message(b"pong", PONG_OPCODE, unit_name=unit)
    t2.join(timeout=4)
    assert _received(results2["pong"])[1] == b"pong"
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
        "unitCode": 112,
        "side": "client",
        "ip": "127.0.0.1",
        "local_ip": "127.0.0.1",
        "connections": {"BeaconUnit": {"port": 17100, "unitCode": 3}},
        "mode": "send_only",
    }
    inbound_cfg = {
        "protocol": "udp",
        "unitCode": 113,
        "side": "server",
        "ip": "127.0.0.1",
        "local_ip": "127.0.0.1",
        "connections": {"BeaconUnit": {"port": 17101, "unitCode": 4}},
        "mode": "receive_only",
    }
    beacon = mgr.create_composite("BeaconUnit", {"transport": outbound_cfg, "receive": inbound_cfg})

    peer_listen_cfg = {
        "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"BeaconUnit": {"port": 17100, "unitCode": 3}}, "mode": "receive_only",
    }
    peer_reply_cfg = {
        "protocol": "udp", "unitCode": 111, "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"BeaconUnit": {"port": 17101, "unitCode": 4}},
    }
    peer_listener = mgr.create("peer_listener", peer_listen_cfg)
    peer_replier = mgr.create("peer_replier", peer_reply_cfg)

    mgr.start_all()

    results = {}
    t1 = _receive_in_background(peer_listener, BEACON_OPCODE, "BeaconUnit", 3, results, "beacon")
    beacon.send_message(b"beacon-out", BEACON_OPCODE)
    t1.join(timeout=4)
    assert _received(results["beacon"])[1] == b"beacon-out"
    print(f"peer received: {_received(results['beacon'])[1]!r}")

    results2 = {}
    t2 = _receive_in_background(beacon, ACK_OPCODE, "BeaconUnit", 3, results2, "ack")
    peer_replier.send_message(b"beacon-ack", ACK_OPCODE, unit_name="BeaconUnit")
    t2.join(timeout=4)
    assert _received(results2["ack"])[1] == b"beacon-ack"
    print(f"BeaconUnit received via its receive-only UDP member: {_received(results2['ack'])[1]!r}")

    try:
        beacon._receiver.send_message(b"should-fail", 0, unit_name="BeaconUnit")
        raise AssertionError("receive-only member should have refused to send")
    except RuntimeError as exc:
        print(f"receive-only member correctly refused to send: {exc}")

    mgr.shutdown_all()
    print("Composite unit fully torn down (both members closed)")


def test_composite_multicast_sender_udp_receiver():
    print("\n=== Composite Unit: real Multicast sender + UDP receiver combined ===")
    BEACON_OPCODE, ACK_OPCODE = 22, 23
    GROUP_IP = "239.1.1.5"
    mgr = ConnectionManager()

    multicast_send_cfg = {
        "protocol": "multicast",
        "unitCode": 120,
        "side": "sender",
        "ip": GROUP_IP,
        "local_ip": "127.0.0.1",
        "connections": {"BeaconUnit": {"port": 17200, "unitCode": 5}},
    }
    udp_receive_cfg = {
        "protocol": "udp",
        "unitCode": 113,
        "side": "server",
        "ip": "127.0.0.1",
        "local_ip": "127.0.0.1",
        "connections": {"BeaconUnit": {"port": 17201, "unitCode": 6}},
        "mode": "receive_only",
    }
    beacon = mgr.create_composite("McastBeaconUnit", {"transport": multicast_send_cfg, "receive": udp_receive_cfg})

    peer_listen_cfg = {
        "protocol": "multicast",
        "unitCode": 121,
        "side": "receiver",
        "ip": GROUP_IP,
        "local_ip": "127.0.0.1",
        "connections": {"BeaconUnit": {"port": 17200, "unitCode": 5}},
    }
    peer_reply_cfg = {
        "protocol": "udp", "unitCode": 111, "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"BeaconUnit": {"port": 17201, "unitCode": 6}},
    }

    try:
        peer_listener = mgr.create("mcast_peer_listener", peer_listen_cfg)
        peer_replier = mgr.create("mcast_peer_replier", peer_reply_cfg)
        mgr.start_all()
    except OSError as exc:
        print(f"skipping: multicast routing unavailable in this environment ({exc})")
        mgr.shutdown_all()
        return

    try:
        results = {}
        t1 = _receive_in_background(peer_listener, BEACON_OPCODE, "BeaconUnit", 3, results, "beacon")
        beacon.send_message(b"mcast-beacon-out", BEACON_OPCODE)
        t1.join(timeout=4)
        if results.get("beacon") is asyncio.TimeoutError:
            print("skipping: no multicast delivery observed in this environment")
            return
        assert _received(results["beacon"])[1] == b"mcast-beacon-out"
        print(f"peer received over multicast: {_received(results['beacon'])[1]!r}")

        results2 = {}
        t2 = _receive_in_background(beacon, ACK_OPCODE, "BeaconUnit", 3, results2, "ack")
        peer_replier.send_message(b"mcast-beacon-ack", ACK_OPCODE, unit_name="BeaconUnit")
        t2.join(timeout=4)
        assert _received(results2["ack"])[1] == b"mcast-beacon-ack"
        print(f"McastBeaconUnit received ack via its UDP receive-only member: "
              f"{_received(results2['ack'])[1]!r}")
    finally:
        mgr.shutdown_all()
        print("Multicast composite unit fully torn down (both members closed)")


def test_message_filtering():
    print("\n=== Message filtering: unsubscribed messages are dropped, not queued ===")
    UNSUBSCRIBED_OPCODE = 77
    mgr = ConnectionManager()

    server_cfg = {
        "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"FilterUnit": {"port": 18000, "unitCode": 5}},
    }
    client_cfg = {
        "protocol": "udp", "unitCode": 111, "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"FilterUnit": {"port": 18000, "unitCode": 5}},
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


def test_echo_is_consumed():
    print("\n=== Inbound echo is consumed: liveness only, never delivered ===")
    ECHO_IN_OPCODE, ECHO_OUT_OPCODE = 99, 100
    mgr = ConnectionManager()

    server_cfg = {
        "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"EchoUnit": {"port": 19000, "unitCode": 6}},
        "recv_echo_opcode": ECHO_IN_OPCODE, "send_echo_opcode": ECHO_OUT_OPCODE,
        # Long interval/timeout: this test is about what happens to an
        # INBOUND echo, so keep the periodic sender out of the way.
        "EchoInterval": 30, "EchoTimeout": 60,
    }
    client_cfg = {
        "protocol": "udp", "unitCode": 111, "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"EchoUnit": {"port": 19000, "unitCode": 6}},
    }
    server = mgr.create("echo_server", server_cfg)
    client = mgr.create("echo_client", client_cfg)
    server.start()
    client.start()
    time.sleep(0.1)

    # A UDP server has no peer address -- and so no liveness clock, and no
    # echo of its own -- until something actually arrives from that unit.
    assert "EchoUnit" not in server._last_echo_at, "liveness tracked before any peer existed"
    before = time.monotonic()
    client.send_message(b"heartbeat", ECHO_IN_OPCODE)
    time.sleep(0.3)

    # 1. It refreshed liveness, which is the entire job of an inbound echo.
    after = server._last_echo_at["EchoUnit"]
    assert after >= before, "inbound echo did not refresh the liveness timestamp"
    print(f"inbound echo refreshed liveness (+{after - before:.3f}s after the send)")

    # 2. It is never visible to the application: echo consumption happens
    #    before subscribe-or-drop even runs, so being subscribed to that
    #    exact opcode changes nothing.
    try:
        server.receive_message(ECHO_IN_OPCODE, timeout=0.5)
        raise AssertionError("echo should have been consumed, not delivered")
    except asyncio.TimeoutError:
        print("confirmed: echo was consumed and never reached the app")

    # 3. No reply was generated: the periodic sender owns the outbound
    #    direction, so an inbound echo must not trigger an extra send.
    try:
        client.receive_message(ECHO_OUT_OPCODE, timeout=0.5)
        raise AssertionError("an inbound echo must not trigger a reply")
    except asyncio.TimeoutError:
        print("confirmed: no reply echo was sent (periodic sender owns that direction)")

    mgr.shutdown_all()
    print("Echo-handling connections fully torn down")


def test_trigger_function_and_callback():
    print("\n=== trigger_function + handle_on_receive (request/response) ===")
    REQUEST_OPCODE, REPLY_OPCODE = 80, 81
    mgr = ConnectionManager()

    responder = mgr.create("responder", {
        "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"AskUnit": {"port": 24000, "unitCode": 13}},
    })
    asker = mgr.create("asker", {
        "protocol": "udp", "unitCode": 111, "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"AskUnit": {"port": 24000, "unitCode": 13}},
    })
    mgr.start_all()

    # The callback calls back into the connection's own SYNC API. That only
    # works because callbacks run on an executor thread -- doing this on the
    # event-loop thread would deadlock, since send_message marshals onto that
    # very thread and waits for it.
    def on_request(message):
        responder.send_message(b"re:" + bytes(message.data), REPLY_OPCODE, unit_name="AskUnit")

    responder.handle_on_receive(REQUEST_OPCODE, on_request, unit_name="AskUnit")

    # trigger_function fires AFTER the subscription is armed, so the reply
    # cannot outrun it -- no background thread, no sleep-before-send needed.
    unit, payload = _received(asker.receive_message(
        REPLY_OPCODE,
        timeout=3,
        trigger_function=lambda: asker.send_message(b"ping", REQUEST_OPCODE),
    ))
    assert payload == b"re:ping", payload
    print(f"request/response completed in one call: {payload!r}")

    # The handler is standing, not one-shot: it answers every request.
    for i in range(3):
        _unit, payload = _received(asker.receive_message(
            REPLY_OPCODE,
            timeout=3,
            trigger_function=lambda i=i: asker.send_message(f"n{i}".encode(), REQUEST_OPCODE),
        ))
        assert payload == f"re:n{i}".encode(), payload
    print("standing callback answered 3 further requests")

    # A route is either polled or handled, never both.
    try:
        responder.handle_on_receive(REQUEST_OPCODE, on_request, unit_name="AskUnit")
        raise AssertionError("registering a second callback should be refused")
    except RuntimeError as exc:
        print(f"confirmed: duplicate callback refused ({exc})")

    assert responder.stop_on_receive(REQUEST_OPCODE, unit_name="AskUnit") is True
    assert responder.stop_on_receive(REQUEST_OPCODE, unit_name="AskUnit") is False
    time.sleep(0.2)
    try:
        asker.receive_message(
            REPLY_OPCODE, timeout=1.0,
            trigger_function=lambda: asker.send_message(b"after-stop", REQUEST_OPCODE),
        )
        raise AssertionError("callback should no longer be registered")
    except asyncio.TimeoutError:
        print("confirmed: stop_on_receive() removed the handler")

    mgr.shutdown_all()
    print("Trigger/callback connections fully torn down")


def test_trigger_function_failure_releases_route():
    print("\n=== A raising trigger_function releases the subscription ===")
    OPCODE = 82
    mgr = ConnectionManager()
    conn = mgr.create("trigger_fail", {
        "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"SoloUnit": {"port": 24500, "unitCode": 14}},
    })
    conn.start()

    def boom():
        raise ValueError("trigger blew up")

    try:
        conn.receive_message(OPCODE, timeout=1, trigger_function=boom)
        raise AssertionError("the trigger's exception should propagate")
    except ValueError as exc:
        print(f"confirmed: trigger exception propagated unchanged ({exc})")

    # The route must not still be claimed by the abandoned subscription.
    try:
        conn.receive_message(OPCODE, timeout=0.3)
        raise AssertionError("expected a clean timeout, not a stale-route error")
    except asyncio.TimeoutError:
        print("confirmed: route was released and can be subscribed again")

    mgr.shutdown_all()
    print("Trigger-failure connection fully torn down")


def test_single_opcode_heartbeat():
    print("\n=== Periodic echo: one shared echo_opcode, no echo storm ===")
    HEARTBEAT_OPCODE = 55
    mgr = ConnectionManager()

    common = {
        "protocol": "udp", "unitCode": 130, "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "echo_opcode": HEARTBEAT_OPCODE, "EchoInterval": 0.3, "EchoTimeout": 5.0,
    }
    peer_a = mgr.create("hb_a", {**common, "side": "server", "connections": {"PeerB": {"port": 21000, "unitCode": 9}}})
    peer_b = mgr.create("hb_b", {**common, "side": "client", "connections": {"PeerA": {"port": 21000, "unitCode": 9}}})

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
        "protocol": "udp", "unitCode": 111, "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"GhostUnit": {"port": 21500, "unitCode": 10}},
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
        "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"TickClient": {"port": 22000, "unitCode": 11}},
    })
    client = mgr.create("tick_client", {
        "protocol": "udp", "unitCode": 111, "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"TickServer": {"port": 22000, "unitCode": 11}},
    })
    mgr.start_all()

    # unit_name omitted: both sides have exactly one connected unit.
    client.periodic_sending(TICK_OPCODE, b"tick", 0.2)
    for i in range(3):
        unit, payload = _received(server.receive_message(TICK_OPCODE, timeout=2))
        assert payload == b"tick", payload
    print("received 3 consecutive ticks from one periodic_sending() call")

    # Task replacement: same (unitCode, opcode) route -> old sender cancelled.
    client.periodic_sending(TICK_OPCODE, b"tock", 0.2)
    time.sleep(0.5)  # let any already-queued "tick" land and be dropped
    for i in range(2):
        unit, payload = _received(server.receive_message(TICK_OPCODE, timeout=2))
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
        "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"BusyUnit": {"port": 23000, "unitCode": 12}},
    })
    server.start()

    results = {}
    t1 = _receive_in_background(server, BUSY_OPCODE, "BusyUnit", 2, results, "first")
    try:
        server.receive_message(BUSY_OPCODE, unitName="BusyUnit", timeout=1)
        raise AssertionError("a second subscriber for the same route should be rejected")
    except RuntimeError as exc:
        print(f"confirmed: duplicate subscription refused ({exc})")
    t1.join(timeout=4)

    mgr.shutdown_all()
    print("Subscription connections fully torn down")


def test_class_handler_routes_message():
    print("\n=== handler_class installs @route methods via handle_on_receive ===")
    REQUEST_OPCODE, REPLY_OPCODE = 30, 31  # registered for unitCode 61 (see test_messages.TEXT_ROUTES)
    mgr = ConnectionManager()

    class EchoBackHandler(UnitHandler):
        unitCode = 61  # RouteUnit's code, as seen from the responder's config

        @route(opCode=REQUEST_OPCODE)
        def handle_request(self, message):
            self.unitConnection.send_message(
                b"re:" + bytes(message.data), REPLY_OPCODE, unit_name="RouteUnit"
            )

    responder = mgr.create("class_handler_responder", {
        "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"RouteUnit": {"port": 25000, "unitCode": 61}},
    }, handler_class=EchoBackHandler)
    asker = mgr.create("class_handler_asker", {
        "protocol": "udp", "unitCode": 111, "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"RouteUnit": {"port": 25000, "unitCode": 61}},
    })
    mgr.start_all()

    unit, payload = _received(asker.receive_message(
        REPLY_OPCODE,
        timeout=3,
        trigger_function=lambda: asker.send_message(b"ping", REQUEST_OPCODE),
    ))
    assert payload == b"re:ping", payload
    print(f"class-routed handler answered: {payload!r}")

    mgr.shutdown_all()
    print("Class-handler connections fully torn down")


def test_class_handler_route_exclusive_with_subscription():
    print("\n=== A class-installed route occupies its slot like an ordinary callback ===")
    ROUTE_OPCODE, OTHER_OPCODE = 30, 31  # registered for unitCode 61
    mgr = ConnectionManager()

    class NoopHandler(UnitHandler):
        unitCode = 61

        @route(opCode=ROUTE_OPCODE)
        def handle_message(self, message):
            pass

    server = mgr.create("class_handler_exclusive", {
        "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"RouteUnit": {"port": 25100, "unitCode": 61}},
    }, handler_class=NoopHandler)

    try:
        server.receive_message(ROUTE_OPCODE, unitName="RouteUnit", timeout=1)
        raise AssertionError("a route already claimed by a class handler should be refused")
    except RuntimeError as exc:
        print(f"confirmed: class-routed opcode refuses a second subscriber ({exc})")

    try:
        server.receive_message(OTHER_OPCODE, unitName="RouteUnit", timeout=0.5)
        raise AssertionError("nothing was sent on OTHER_OPCODE; this should time out, not error")
    except asyncio.TimeoutError:
        print("confirmed: an unrelated opcode on the same connection is unaffected")

    mgr.shutdown_all()
    print("Class-handler-exclusivity connections fully torn down")


def test_class_handler_unknown_unit_code_raises():
    print("\n=== handler_class.unitCode with no matching configured unit fails atomically ===")
    mgr = ConnectionManager()

    class OrphanHandler(UnitHandler):
        unitCode = 0xFE  # no configured connection ever uses this code

        @route(opCode=30)
        def handle_message(self, message):
            pass

    try:
        mgr.create("orphan_handler_conn", {
            "protocol": "udp", "unitCode": 110, "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
            "connections": {"RouteUnit": {"port": 25200, "unitCode": 61}},
        }, handler_class=OrphanHandler)
        raise AssertionError("an unmatched handler unitCode should be rejected")
    except ValueError as exc:
        print(f"confirmed: unmatched handler unitCode refused ({exc})")

    try:
        mgr.get("orphan_handler_conn")
        raise AssertionError("a connection that failed handler installation should not be registered")
    except KeyError:
        print("confirmed: the failed connection was never registered with the manager")

    mgr.shutdown_all()


class _WarningTrap(logging.Handler):
    """Collects framework warnings so a test can assert on what was NOT logged."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def __enter__(self):
        logging.getLogger("connmgr").addHandler(self)
        return self

    def __exit__(self, *exc_info):
        logging.getLogger("connmgr").removeHandler(self)


def test_echo_follows_peer_connection():
    print("\n=== Echo lifecycle follows the actual peer, per unit ===")
    mgr = ConnectionManager()
    common = {
        "protocol": "tcp", "unitCode": 133, "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "echo_opcode": 99, "EchoInterval": 0.2, "EchoTimeout": 1.0,
    }
    server = mgr.create("echo_lifecycle_server", {
        **common, "side": "server",
        "connections": {"unit1": {"port": 18700, "unitCode": 41},
                        "unit2": {"port": 18701, "unitCode": 42}},
    })
    client_cfg = {**common, "side": "client",
                  "connections": {"unit1": {"port": 18700, "unitCode": 41}}}

    # Count real send attempts per unit. Asserting on what was *logged* would
    # be too weak: an echo aimed at a unit with no peer is now reported at
    # INFO, so only the send count proves none was attempted at all.
    attempts: dict[str, int] = {}
    original_send = server._do_send

    async def counting_send(unit_name, data, opcode):
        attempts[unit_name] = attempts.get(unit_name, 0) + 1
        return await original_send(unit_name, data, opcode)

    server._do_send = counting_send

    with _WarningTrap() as trap:
        server.start()
        # Nothing has connected, so nothing may be echoed at -- and nothing
        # may be watchdogged into a disconnect either.
        assert server.active_units == set(), server.active_units
        assert server.wait_for_connected_units(1, timeout=0.8) is False
        assert not attempts, f"echoes were sent before any peer connected: {attempts}"
        assert not trap.messages, trap.messages
        print("no echo attempted, and no unit dropped, while both units were unconnected")

        client = mgr.create("echo_lifecycle_client", client_cfg)
        client.start()
        assert server.wait_for_connected_units("unit1", timeout=3) is True
        assert server.active_units == {"unit1"}, server.active_units
        assert server._echo_tasks.get("unit1"), "unit1's echo should be armed on connect"
        assert not server._echo_tasks.get("unit2"), "unit2 has no peer; it must stay disarmed"

        # Past EchoTimeout: the running heartbeat is what keeps unit1 up.
        time.sleep(1.4)
        assert server.active_units == {"unit1"}, "heartbeat failed to keep the unit alive"
        assert attempts.get("unit1", 0) > 0, "unit1's heartbeat never ran"
        assert "unit2" not in attempts, f"unit2 has no peer but was echoed at: {attempts}"
        assert not trap.messages, trap.messages
        print(f"unit1's echo armed on connect ({attempts['unit1']} sends); unit2 left alone")

        client.close()
        time.sleep(0.4)
        assert server.active_units == set(), "a closed peer must mark its unit down"
        assert not server._echo_tasks.get("unit1"), "echo must stop when the peer drops"
        settled = dict(attempts)
        time.sleep(0.6)  # 3 intervals during which nothing may be sent
        assert attempts == settled, f"echoes continued after disconnect: {attempts} vs {settled}"
        assert "unit2" not in attempts, attempts
        print("echo stopped cleanly when the peer disconnected")

        reconnected = mgr.create("echo_lifecycle_client2", client_cfg)
        reconnected.start()
        assert server.wait_for_connected_units("unit1", timeout=3) is True
        time.sleep(1.4)  # past EchoTimeout again, on the resumed heartbeat
        assert server.active_units == {"unit1"}, "echo did not resume on reconnect"
        assert attempts["unit1"] > settled["unit1"], "echo did not resume on reconnect"
        assert "unit2" not in attempts, attempts
        print("echo resumed automatically on reconnect")

    mgr.shutdown_all()
    print("Echo-lifecycle connections fully torn down")


def test_wait_for_connected_units():
    print("\n=== wait_for_connected_units: int / str / list targets ===")
    mgr = ConnectionManager()
    common = {"protocol": "tcp", "unitCode": 131, "ip": "127.0.0.1", "local_ip": "127.0.0.1"}
    units = {"AlphaUnit": {"port": 18800, "unitCode": 51},
             "BetaUnit": {"port": 18801, "unitCode": 52}}
    server = mgr.create("wait_server", {**common, "side": "server", "connections": units})
    server.start()

    assert server.wait_for_connected_units(0) is True  # trivially satisfied
    assert server.wait_for_connected_units(1, timeout=0.3) is False
    assert server.wait_for_connected_units("AlphaUnit", timeout=0.3) is False
    print("confirmed: unmet targets time out and return False")

    for bad, kind in ((5, "more units than configured"), ("NoSuchUnit", "unknown name"),
                      (["AlphaUnit", "Ghost"], "unknown name in a list")):
        try:
            server.wait_for_connected_units(bad, timeout=0.1)
            raise AssertionError(f"expected {kind} to be rejected")
        except ValueError:
            pass
    print("confirmed: impossible targets are rejected up front, not waited on")

    alpha = mgr.create("wait_alpha", {**common, "side": "client",
                                      "connections": {"AlphaUnit": units["AlphaUnit"]}})
    alpha.start()
    assert server.wait_for_connected_units("AlphaUnit", timeout=3) is True
    assert server.wait_for_connected_units(["AlphaUnit", "BetaUnit"], timeout=0.3) is False

    # Block first, connect second: the waiter must be released by the event,
    # having never polled for it.
    results = {}
    waiter = threading.Thread(
        target=lambda: results.update(
            both=server.wait_for_connected_units(["AlphaUnit", "BetaUnit"], timeout=3)
        ),
        daemon=True,
    )
    waiter.start()
    time.sleep(0.3)
    beta = mgr.create("wait_beta", {**common, "side": "client",
                                    "connections": {"BetaUnit": units["BetaUnit"]}})
    beta.start()
    waiter.join(timeout=4)
    assert results.get("both") is True, results
    assert server.active_units == {"AlphaUnit", "BetaUnit"}, server.active_units
    assert server.wait_for_connected_units(2) is True
    print("a parked waiter was released the moment the last unit connected")

    mgr.shutdown_all()
    print("Wait-for-units connections fully torn down")


def test_own_unit_code():
    print("\n=== Own unitCode: required, and stamped into what we send ===")
    base_cfg = {"protocol": "udp", "side": "client", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
                "connections": {"ThemUnit": {"port": 19300, "unitCode": 21}}}
    mgr = ConnectionManager()

    try:
        mgr.create("no_own_code", base_cfg)
        raise AssertionError("a config without its own unitCode should be rejected")
    except ValueError as exc:
        print(f"confirmed: own unitCode is mandatory ({str(exc).splitlines()[0][:60]}...)")

    for bad in (256, -1, "seven"):
        try:
            mgr.create("bad_own_code", {**base_cfg, "unitCode": bad})
            raise AssertionError(f"unitCode={bad!r} should be rejected")
        except ValueError:
            pass
    print("confirmed: own unitCode is range- and type-checked at load time")

    conn = mgr.create("own_code", {**base_cfg, "unitCode": 77})
    assert conn.config.unitCode == 77
    assert conn.config.unit_code_for("ThemUnit") == 21  # theirs, untouched

    # The header carries OUR code, not the destination unit's.
    header = unpack_header(conn._frame("ThemUnit", b"payload", opcode=5))
    assert header.unit_code == 77, f"expected our own code 77, got {header.unit_code}"
    assert header.opcode == 5 and header.data_length == 7
    print(f"confirmed: header stamps our code ({header.unit_code}), not the peer's (21)")

    mgr.shutdown_all()


#: Where the IRS message layouts for the test below live. Named by the configs
#: under "Structures", so ConnectionManager.create() is what imports it -- and
#: importing it is what runs its register_message() calls, under this module
#: name as their namespace.
IRS_MESSAGE_LIB = "core.IRS.Structures.Test.test_messages"


def _track_report(track_id, kind, heading):
    """Build one IRS TrackReport. Imported lazily, because the whole point of
    the test below is that the layouts arrive via the config's Structures."""
    from core.IRS.Structures.Test.test_messages import E_TrackKind, TrackReport
    message = TrackReport()
    message.trackId = track_id
    message.kind = E_TrackKind[kind]
    message.heading = heading
    return message


def test_irs_parser_roundtrip():
    print("\n=== IRS parsing: registered messages in, bytes on the wire ===")
    # These must match what IRS_MESSAGE_LIB registers: the module registers
    # TrackReport under the client's code and TrackAck under the server's,
    # both on this one opcode.
    OPCODE = 60
    SERVER_CODE, CLIENT_CODE = 22, 21
    from core.IRS.REGISTRY import get_specification

    mgr = ConnectionManager()
    try:
        common = {"protocol": "udp", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
                  "Structures": [IRS_MESSAGE_LIB]}
        # Each side declares its own code, and names the other's under the
        # unit it talks to -- so "ours" and "theirs" are genuinely different.
        server = mgr.create("irs_server", {
            **common, "side": "server", "unitCode": SERVER_CODE,
            "connections": {"Peer": {"port": 19200, "unitCode": CLIENT_CODE}}})
        client = mgr.create("irs_client", {
            **common, "side": "client", "unitCode": CLIENT_CODE,
            "connections": {"Peer": {"port": 19200, "unitCode": SERVER_CODE}}})

        # create() imports the config's Structures, so register_message() has
        # run before either connection can receive anything. Both layouts live
        # in that module's namespace -- one file, one link, both directions.
        registered = get_specification(IRS_MESSAGE_LIB)
        assert registered[CLIENT_CODE][OPCODE].__name__ == "TrackReport", registered
        assert registered[SERVER_CODE][OPCODE].__name__ == "TrackAck", registered
        print(f"Structures registered both layouts on opcode {OPCODE}: "
              f"unit {CLIENT_CODE}=TrackReport, unit {SERVER_CODE}=TrackAck")

        from core.IRS.Structures.Test.test_messages import E_TrackKind, TrackAck, TrackReport
        mgr.start_all()

        report = _track_report(4097, "AIR", 270)
        results = {}
        t = _receive_in_background(server, OPCODE, None, 3, results, "obj")
        client.send_message(report, OPCODE)
        t.join(timeout=4)
        unit, received = results["obj"]
        assert unit == "Peer", results
        assert isinstance(received, TrackReport), received
        assert received.to_dict() == report.to_dict(), received.to_dict()
        # 5 bytes of struct-packed IRS, not a repr: the object was rebuilt from
        # the wire, not handed across in-process.
        assert len(report.to_bytes()) == 5, report.to_bytes()
        print(f"sent a TrackReport, received it back parsed: {received.to_dict()}")

        # Reply path: SAME opcode, and only the sender's unit code differs --
        # so getting a TrackAck back is proof the parser keyed on the sender's
        # code (22) rather than on the receiver's own.
        ack = TrackAck()
        ack.trackId = 4097
        ack.accepted = 1
        results_r = {}
        t = _receive_in_background(client, OPCODE, None, 3, results_r, "reply")
        server.send_message(ack, OPCODE, unit_name="Peer")
        t.join(timeout=4)
        _unit, reply = results_r["reply"]
        assert isinstance(reply, TrackAck), reply
        assert reply.to_dict() == ack.to_dict(), reply.to_dict()
        print(f"confirmed: opcode {OPCODE} parsed as TrackReport from unit {CLIENT_CODE} and as "
              f"TrackAck from unit {SERVER_CODE} -- the sender's code selects the layout")

        # One byte where TrackReport's first field alone needs two: the parse
        # fails and the waiting subscriber is handed the failure.
        results2 = {}
        t = _receive_in_background(server, OPCODE, None, 2, results2, "bad")
        client.send_message(b"\x01", OPCODE)
        t.join(timeout=3)
        assert isinstance(results2["bad"], BufferError), results2
        print(f"confirmed: an unparseable payload raised {type(results2['bad']).__name__}")

        after = _track_report(9, "SURFACE", 45)
        results3 = {}
        t = _receive_in_background(server, OPCODE, None, 3, results3, "after")
        client.send_message(after, OPCODE)
        t.join(timeout=4)
        assert results3["after"][1].to_dict() == after.to_dict(), results3
        print("confirmed: the connection survived the malformed message")

        # Standing callbacks and periodic sends use the same codec.
        tick = _track_report(12, "UNKNOWN", 180)
        received_msgs: list[TrackReport] = []
        server.handle_on_receive(OPCODE, received_msgs.append)
        client.periodic_sending(OPCODE, tick, 0.2)
        time.sleep(0.7)
        client.stop_periodic(OPCODE)
        assert received_msgs, "no periodic messages arrived"
        assert all(m.to_dict() == tick.to_dict() for m in received_msgs), \
            [m.to_dict() for m in received_msgs]
        assert all(m.kind is E_TrackKind.UNKNOWN for m in received_msgs), received_msgs
        print(f"handle_on_receive + periodic_sending delivered "
              f"{len(received_msgs)} parsed TrackReport objects")
    finally:
        mgr.shutdown_all()
    print("IRS parsing connections fully torn down")


def test_hierarchical_echo_config():
    print("\n=== Echo config hierarchy: connection default, per-unit override ===")

    # -- Part 1: resolution. No sockets involved; this is purely about what
    #    ConnectionConfig.from_json produces for each unit.
    base_cfg = {
        "protocol": "tcp", "side": "server", "ip": "127.0.0.1", "unitCode": 3,
        "echo_opcode": 99, "EchoInterval": 1.0, "EchoTimeout": 5.0,
    }
    cfg = ConnectionConfig.from_json({
        **base_cfg,
        "connections": {
            # own opcode, inherited timings
            "RadarUnit":   {"port": 15200, "unitCode": 7, "echo_opcode": 10},
            # nothing of its own: pure fallback
            "TrackerUnit": {"port": 15201, "unitCode": 8},
            # explicit opt-out of a heartbeat everyone else has
            "SilentUnit":  {"port": 15202, "unitCode": 9, "echo_opcode": None},
            # asymmetric opcodes + one overridden timing, the rest inherited
            "AsymUnit":    {"port": 15203, "unitCode": 11,
                            "recv_echo_opcode": 20, "send_echo_opcode": 21,
                            "EchoTimeout": 9.0},
        },
    })

    radar = cfg.echo_for("RadarUnit")
    assert (radar.recv_opcode, radar.send_opcode) == (10, 10), radar
    assert (radar.interval, radar.timeout) == (1.0, 5.0), radar
    tracker = cfg.echo_for("TrackerUnit")
    assert (tracker.recv_opcode, tracker.send_opcode) == (99, 99), tracker
    assert (tracker.interval, tracker.timeout) == (1.0, 5.0), tracker
    assert not cfg.echo_for("SilentUnit").enabled, "explicit null must disable that unit"
    asym = cfg.echo_for("AsymUnit")
    assert (asym.recv_opcode, asym.send_opcode) == (20, 21), asym
    assert (asym.interval, asym.timeout) == (1.0, 9.0), "tuning keys resolve individually"
    print("per-unit opcodes override, unset units inherit, null opts out, timings still shared")

    # The three opcode keys resolve as a GROUP: a unit naming one of them must
    # not inherit the other direction from the connection level, which would
    # build a link neither peer configured.
    grouped = ConnectionConfig.from_json({
        "protocol": "tcp", "side": "server", "ip": "127.0.0.1", "unitCode": 3,
        "recv_echo_opcode": 99, "send_echo_opcode": 98,
        "connections": {"U": {"port": 15210, "unitCode": 12, "echo_opcode": 10}},
    })
    resolved = grouped.echo_for("U")
    assert (resolved.recv_opcode, resolved.send_opcode) == (10, 10), resolved
    print("confirmed: a unit's opcodes replace the global set outright, never half of it")

    # Absent at both levels -> that unit simply doesn't heartbeat.
    bare = ConnectionConfig.from_json({
        "protocol": "tcp", "side": "server", "ip": "127.0.0.1", "unitCode": 3,
        "connections": {"U": {"port": 15211, "unitCode": 13}},
    })
    assert not bare.echo_for("U").enabled, "echo must stay off with no opcodes anywhere"

    # A merge that produces an impossible config still fails at load time, and
    # says which unit produced it.
    try:
        ConnectionConfig.from_json({
            "protocol": "tcp", "side": "server", "ip": "127.0.0.1", "unitCode": 3,
            "echo_opcode": 99, "EchoInterval": 1.0,
            "connections": {"BadUnit": {"port": 15212, "unitCode": 14, "EchoTimeout": 0.5}},
        })
        raise AssertionError("a unit-level EchoTimeout <= EchoInterval should not load")
    except ValueError as exc:
        assert "BadUnit" in str(exc), exc
        print(f"confirmed: bad per-unit echo fails at load, naming the unit ({str(exc)[:60]}...)")

    # -- Part 2: behaviour. Two units on ONE connection, heartbeating on
    #    different opcodes, must send and consume independently.
    GLOBAL_ECHO, UNIT1_ECHO = 30, 31
    mgr = ConnectionManager()
    common = {"protocol": "tcp", "ip": "127.0.0.1", "local_ip": "127.0.0.1"}
    server = mgr.create("hier_echo_server", {
        **common, "side": "server", "unitCode": 160,
        "connections": {"unit1": {"port": 18900, "unitCode": 61, "echo_opcode": UNIT1_ECHO},
                        "unit2": {"port": 18901, "unitCode": 62}},
        "echo_opcode": GLOBAL_ECHO, "EchoInterval": 0.2,
        # Long timeout: the peers below never echo back, and the watchdog is
        # not what this test is about.
        "EchoTimeout": 30,
    })

    # Record the opcode of every echo the server actually puts on the wire.
    sent: dict[str, set[int]] = {}
    original_send = server._do_send

    async def recording_send(unit_name, data, opcode):
        sent.setdefault(unit_name, set()).add(opcode)
        return await original_send(unit_name, data, opcode)

    server._do_send = recording_send

    # Clients carry no echo keys at all, so they stay silent and every echo
    # observed below is unambiguously the server's.
    client1 = mgr.create("hier_echo_client1", {
        **common, "side": "client", "unitCode": 161,
        "connections": {"unit1": {"port": 18900, "unitCode": 61}}})
    client2 = mgr.create("hier_echo_client2", {
        **common, "side": "client", "unitCode": 162,
        "connections": {"unit2": {"port": 18901, "unitCode": 62}}})

    try:
        server.start()
        client1.start()
        client2.start()
        assert server.wait_for_connected_units(["unit1", "unit2"], timeout=3) is True
        time.sleep(0.7)  # a few intervals of both heartbeats

        assert sent.get("unit1") == {UNIT1_ECHO}, f"unit1 echoed on {sent.get('unit1')}"
        assert sent.get("unit2") == {GLOBAL_ECHO}, f"unit2 echoed on {sent.get('unit2')}"
        print(f"each unit heartbeats on its own opcode: unit1={UNIT1_ECHO}, unit2={GLOBAL_ECHO}")

        # Inbound: the same opcode is a heartbeat on one unit and an ordinary
        # application message on the other, decided per unit.
        results = {}
        t = _receive_in_background(server, GLOBAL_ECHO, "unit1", 2, results, "app")
        client1.send_message(b"not-an-echo", GLOBAL_ECHO, unit_name="unit1")
        t.join(timeout=3)
        assert _received(results["app"]) == ("unit1", b"not-an-echo"), results
        print("the global echo opcode is delivered as data on the unit that overrode it")

        client1.send_message(b"heartbeat", UNIT1_ECHO, unit_name="unit1")
        time.sleep(0.3)
        try:
            server.receive_message(UNIT1_ECHO, unitName="unit1", timeout=0.5)
            raise AssertionError("unit1's own echo opcode should have been consumed")
        except asyncio.TimeoutError:
            print("unit1's overridden opcode is consumed as an echo, never delivered")

        client2.send_message(b"heartbeat", GLOBAL_ECHO, unit_name="unit2")
        time.sleep(0.3)
        try:
            server.receive_message(GLOBAL_ECHO, unitName="unit2", timeout=0.5)
            raise AssertionError("unit2's inherited echo opcode should have been consumed")
        except asyncio.TimeoutError:
            print("unit2's inherited opcode is consumed as an echo on the same connection")
    finally:
        mgr.shutdown_all()
    print("Hierarchical-echo connections fully torn down")


if __name__ == "__main__":
    test_tcp_roundtrip()
    test_udp_single_unit()
    test_composite_unit()
    test_composite_multicast_sender_udp_receiver()
    test_message_filtering()
    test_echo_is_consumed()
    test_trigger_function_and_callback()
    test_trigger_function_failure_releases_route()
    test_single_opcode_heartbeat()
    test_echo_timeout_disconnect()
    test_echo_follows_peer_connection()
    test_hierarchical_echo_config()
    test_wait_for_connected_units()
    test_own_unit_code()
    test_irs_parser_roundtrip()
    test_periodic_sending()
    test_single_subscription_per_route()
    test_class_handler_routes_message()
    test_class_handler_route_exclusive_with_subscription()
    test_class_handler_unknown_unit_code_raises()
    print("\nALL TESTS PASSED")
