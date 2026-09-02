"""
connections/udp.py -- implicit single-unit send, send/receive_only mode,
server-side peer learning, malformed-datagram tolerance.
"""
import socket
import time

import pytest

from core.tests._messages import TEXT_UNIT_CODE, TEXT_UNIT_CODE_2


def test_unit_name_optional_with_a_single_connected_unit(manager, free_port):
    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    client = manager.create("client", {
        "protocol": "udp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    manager.start_all()

    unit, message = server.receive_message(
        1, timeout=3,  # unit_name omitted -- only one unit configured
        trigger_function=lambda: client.send_message(b"hi", 1),  # unit_name omitted too
    )
    assert unit == "Peer"
    assert bytes(message.data) == b"hi"


def test_unit_name_required_with_multiple_units(manager, free_ports):
    port_a, port_b = free_ports(2)
    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {
            "A": {"port": port_a, "unitCode": TEXT_UNIT_CODE},
            "B": {"port": port_b, "unitCode": TEXT_UNIT_CODE_2},
        },
    })
    server.start()
    with pytest.raises(ValueError, match="unit_name"):
        server.receive_message(1, timeout=0.3)


def test_send_only_mode_cannot_receive_and_receive_only_cannot_send(manager, free_ports):
    """`mode` only gates the SEND path (`_do_send` raises `RuntimeError` when
    `can_send` is False -- see udp.py). `can_receive` governs whether the
    echo watchdog arms itself (base.py `_start_unit_echo`), not whether
    `receive_message()` may be called at all -- a send_only connection can
    still subscribe, it just never gets anything because nothing valid ever
    routes traffic to it in a real deployment."""
    port_send, port_recv = free_ports(2)
    sender = manager.create("sender", {
        "protocol": "udp", "unitCode": 100, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1", "mode": "send_only",
        "connections": {"Peer": {"port": port_send, "unitCode": TEXT_UNIT_CODE}},
    })
    receiver = manager.create("receiver", {
        "protocol": "udp", "unitCode": 101, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1", "mode": "receive_only",
        "connections": {"Peer": {"port": port_recv, "unitCode": TEXT_UNIT_CODE}},
    })
    manager.start_all()

    assert sender.can_send is True and sender.can_receive is False
    assert receiver.can_send is False and receiver.can_receive is True

    with pytest.raises(RuntimeError):
        receiver.send_message(b"nope", 1, unit_name="Peer")


def test_invalid_mode_rejected_at_construction(manager, free_port):
    with pytest.raises(ValueError, match="mode"):
        manager.create("c", {
            "protocol": "udp", "unitCode": 100, "side": "server",
            "ip": "127.0.0.1", "local_ip": "127.0.0.1", "mode": "sideways",
            "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
        })


def test_server_learns_peer_address_from_first_datagram(manager, free_port):
    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    server.start()
    assert "Peer" not in server.active_units

    client = manager.create("client", {
        "protocol": "udp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    client.start()
    client.send_message(b"hello", 1)

    assert server.wait_for_connected_units("Peer", timeout=3) is True


def test_server_send_before_any_inbound_datagram_raises(manager, free_port):
    """A UDP server has no learned peer address until it has received
    something -- sending to it beforehand must fail loudly, not corrupt the
    unconnected socket's write buffer."""
    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    server.start()
    with pytest.raises(ConnectionError):
        server.send_message(b"nobody-yet", 1, unit_name="Peer")


def test_malformed_datagram_is_dropped_not_fatal(manager, free_port):
    from core.connections.framing import HEADER_SIZE

    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    client = manager.create("client", {
        "protocol": "udp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    manager.start_all()

    # Fewer bytes than one header -- unpack_message raises inside
    # datagram_received, which must catch it and keep the socket alive.
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    raw_sock.sendto(b"\x00\x01", ("127.0.0.1", free_port))
    raw_sock.close()
    time.sleep(0.2)

    # The connection is still fully usable afterwards.
    unit, message = server.receive_message(
        1, unit_name="Peer", timeout=3,
        trigger_function=lambda: client.send_message(b"still-works", 1),
    )
    assert bytes(message.data) == b"still-works"


def test_connection_not_started_raises_on_send(manager, free_port):
    connection = manager.create("c", {
        "protocol": "udp", "unitCode": 100, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })
    # Never started -- no transport exists yet.
    with pytest.raises(Exception):
        connection.send_message(b"x", 1, unit_name="Peer")
