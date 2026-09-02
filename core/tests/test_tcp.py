"""
connections/tcp.py -- multi-port server, header framing over a byte stream
(no message boundaries of its own), reconnect, per-unit disconnect isolation.
"""
import time

import pytest

from core.tests._messages import TEXT_UNIT_CODE, TEXT_UNIT_CODE_2


def test_multi_unit_round_trip(manager, free_ports):
    port_radar, port_tracker = free_ports(2)
    server = manager.create("server", {
        "protocol": "tcp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {
            "Radar": {"port": port_radar, "unitCode": TEXT_UNIT_CODE},
            "Tracker": {"port": port_tracker, "unitCode": TEXT_UNIT_CODE_2},
        },
    })
    radar = manager.create("radar", {
        "protocol": "tcp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Radar": {"port": port_radar, "unitCode": TEXT_UNIT_CODE}},
    })
    tracker = manager.create("tracker", {
        "protocol": "tcp", "unitCode": 102, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Tracker": {"port": port_tracker, "unitCode": TEXT_UNIT_CODE_2}},
    })
    manager.start_all()
    assert server.wait_for_connected_units(["Radar", "Tracker"], timeout=3) is True

    unit, message = server.receive_message(
        1, unit_name="Radar", timeout=3,
        trigger_function=lambda: radar.send_message(b"hello-radar", 1),
    )
    assert (unit, bytes(message.data)) == ("Radar", b"hello-radar")

    unit, message = server.receive_message(
        1, unit_name="Tracker", timeout=3,
        trigger_function=lambda: tracker.send_message(b"hello-tracker", 1),
    )
    assert (unit, bytes(message.data)) == ("Tracker", b"hello-tracker")


def test_message_larger_than_one_packet_reassembles_correctly(manager, free_port):
    """TCP gives no message boundaries -- the read loop must read exactly
    DataLength bytes regardless of how the OS chose to chunk them."""
    server = manager.create("server", {
        "protocol": "tcp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    client = manager.create("client", {
        "protocol": "tcp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    manager.start_all()
    assert server.wait_for_connected_units("Peer", timeout=3) is True

    big_payload = bytes(range(256)) * 200  # 51200 bytes, well over typical MTU
    unit, message = server.receive_message(
        1, unit_name="Peer", timeout=5,
        trigger_function=lambda: client.send_message(big_payload, 1),
    )
    assert bytes(message.data) == big_payload


def test_new_client_supersedes_the_previous_peer_and_rearms_echo(manager, free_port):
    """A second inbound connection on the same unit's port replaces the
    first as that unit's active send target."""
    server = manager.create("server", {
        "protocol": "tcp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    server.start()

    client_a = manager.create("client_a", {
        "protocol": "tcp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    client_a.start()
    assert server.wait_for_connected_units("Peer", timeout=3) is True

    client_b = manager.create("client_b", {
        "protocol": "tcp", "unitCode": 102, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    client_b.start()
    time.sleep(0.3)

    # The server's next send for "Peer" must go out on client_b's socket.
    unit, message = client_b.receive_message(
        1, unit_name="Peer", timeout=3,
        trigger_function=lambda: server.send_message(b"to-newest-peer", 1, unit_name="Peer"),
    )
    assert bytes(message.data) == b"to-newest-peer"


def test_peer_closing_marks_the_unit_disconnected(manager, free_port):
    server = manager.create("server", {
        "protocol": "tcp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    client = manager.create("client", {
        "protocol": "tcp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    manager.start_all()
    assert server.wait_for_connected_units("Peer", timeout=3) is True

    client.close()
    deadline = time.monotonic() + 3
    while "Peer" in server.active_units and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "Peer" not in server.active_units


def test_send_with_no_active_peer_raises_connection_error(manager, free_port):
    server = manager.create("server", {
        "protocol": "tcp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    server.start()
    with pytest.raises(ConnectionError):
        server.send_message(b"nobody-there", 1, unit_name="Peer")


def test_own_unit_code_is_stamped_in_the_header_not_the_peers(manager, free_port):
    from core.connections.framing import unpack_header

    connection = manager.create("c", {
        "protocol": "tcp", "unitCode": 77, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": 21}},
    })
    header = unpack_header(connection._frame("Peer", b"payload", opcode=5))
    assert header.unit_code == 77
    assert header.opcode == 5
    assert header.data_length == 7
