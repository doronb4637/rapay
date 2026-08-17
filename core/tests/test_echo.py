"""
connections/base.py -- echo lifecycle: periodic sender + liveness watchdog,
armed/disarmed strictly by real peer connect/disconnect state. Real-time
tests (short intervals/timeouts, actual sleeps) -- marked `slow`.
"""
import time

import pytest

from core.tests._messages import TEXT_UNIT_CODE, TEXT_UNIT_CODE_2

pytestmark = pytest.mark.slow


def test_echo_disconnects_a_silent_peer(manager, free_port):
    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
        "echo_opcode": 15, "EchoInterval": 0.15, "EchoTimeout": 0.5,
    })
    client = manager.create("client", {
        "protocol": "udp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
        # No echo keys on the client -- it never answers, so the server's
        # watchdog is the only thing that can end this.
    })
    manager.start_all()

    client.send_message(b"hello", 1)  # server learns the peer's address
    assert server.wait_for_connected_units("Peer", timeout=2) is True

    time.sleep(1.0)  # comfortably past EchoTimeout with zero replies
    assert "Peer" not in server.active_units


def test_echo_keeps_a_responsive_peer_alive(manager, free_port):
    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
        "echo_opcode": 15, "EchoInterval": 0.15, "EchoTimeout": 0.6,
    })
    client = manager.create("client", {
        "protocol": "udp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
        "echo_opcode": 15, "EchoInterval": 0.15, "EchoTimeout": 0.6,
    })
    manager.start_all()

    assert server.wait_for_connected_units("Peer", timeout=2) is True
    time.sleep(1.2)  # several EchoTimeout windows, both sides echoing
    assert "Peer" in server.active_units
    assert "Peer" in client.active_units


def test_echo_opcode_never_reaches_receive_message(manager, free_port):
    """Real periodic echo traffic flowing both directions must still never
    surface to receive_message() on either end -- consumption is per the
    RECEIVING side's own echo config, regardless of who initiated it."""
    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
        "echo_opcode": 15, "EchoInterval": 0.1, "EchoTimeout": 30,
    })
    client = manager.create("client", {
        "protocol": "udp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
        "echo_opcode": 15, "EchoInterval": 0.1, "EchoTimeout": 30,
    })
    manager.start_all()
    assert server.wait_for_connected_units("Peer", timeout=2) is True

    time.sleep(0.5)  # several real echoes fired in both directions by now
    with pytest.raises(TimeoutError):
        server.receive_message(15, unitName="Peer", timeout=0.3)
    with pytest.raises(TimeoutError):
        client.receive_message(15, unitName="Peer", timeout=0.3)


def test_hierarchical_echo_per_unit_opcode_override(manager, free_ports):
    """Two units on one connection heartbeat on distinct opcodes -- a
    per-unit override must not bleed into the sibling unit's traffic."""
    port1, port2 = free_ports(2)
    GLOBAL_ECHO, UNIT1_ECHO = 15, 16
    server = manager.create("server", {
        "protocol": "tcp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {
            "unit1": {"port": port1, "unitCode": TEXT_UNIT_CODE, "echo_opcode": UNIT1_ECHO},
            "unit2": {"port": port2, "unitCode": TEXT_UNIT_CODE_2},
        },
        "echo_opcode": GLOBAL_ECHO, "EchoInterval": 0.15, "EchoTimeout": 30,
    })
    sent: dict[str, set[int]] = {}
    original_send = server._do_send

    async def recording_send(unit_name, data, opcode):
        sent.setdefault(unit_name, set()).add(opcode)
        return await original_send(unit_name, data, opcode)
    server._do_send = recording_send

    client1 = manager.create("client1", {
        "protocol": "tcp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"unit1": {"port": port1, "unitCode": TEXT_UNIT_CODE}},
    })
    client2 = manager.create("client2", {
        "protocol": "tcp", "unitCode": 102, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"unit2": {"port": port2, "unitCode": TEXT_UNIT_CODE_2}},
    })
    manager.start_all()
    assert server.wait_for_connected_units(["unit1", "unit2"], timeout=3) is True

    time.sleep(0.6)
    assert sent.get("unit1") == {UNIT1_ECHO}
    assert sent.get("unit2") == {GLOBAL_ECHO}


def test_echo_watchdog_disconnects_only_the_dead_unit(manager, free_ports):
    """A two-unit TCP server: one peer stops answering, the other stays
    alive -- the watchdog must isolate the failure to one unit."""
    port1, port2 = free_ports(2)
    server = manager.create("server", {
        "protocol": "tcp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {
            "silent": {"port": port1, "unitCode": TEXT_UNIT_CODE},
            "alive": {"port": port2, "unitCode": TEXT_UNIT_CODE_2},
        },
        "echo_opcode": 15, "EchoInterval": 0.15, "EchoTimeout": 0.5,
    })
    client_silent = manager.create("client_silent", {
        "protocol": "tcp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"silent": {"port": port1, "unitCode": TEXT_UNIT_CODE}},
        # No echo keys -- never answers the server's heartbeat.
    })
    client_alive = manager.create("client_alive", {
        "protocol": "tcp", "unitCode": 102, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"alive": {"port": port2, "unitCode": TEXT_UNIT_CODE_2}},
        "echo_opcode": 15, "EchoInterval": 0.15, "EchoTimeout": 30,
    })
    manager.start_all()
    assert server.wait_for_connected_units(["silent", "alive"], timeout=3) is True

    time.sleep(1.0)
    assert "silent" not in server.active_units
    assert "alive" in server.active_units
