"""
connections/composite.py -- CompositeUnit combines direction-limited members
into one logical Unit. Uses two directional UDP links in place of true
multicast, same substitution `connections/test_framework.py` documents (this
sandbox's network namespace has no multicast routing).
"""
import pytest

from core.connections.composite import CompositeUnit
from core.connections.manager import ConnectionManager
from core.tests._messages import TEXT_UNIT_CODE


def _member(protocol_mode, port, own_code=TEXT_UNIT_CODE, peer_code=TEXT_UNIT_CODE, side="client"):
    """Both `own_code` and `peer_code` default to the same registered
    TEXT_UNIT_CODE: `_encode` keys off the SENDER's own top-level `unitCode`,
    `_decode` keys off the RECEIVER's declared entry for that peer -- for a
    message to actually round-trip through IRS, whichever of those two is
    live for a given send must resolve to a registered code, so keeping both
    on the one registered value sidesteps having to reason per-direction
    about which one is exercised in a given test."""
    return {
        "protocol": "udp", "unitCode": own_code, "side": side,
        "ip": "127.0.0.1", "local_ip": "127.0.0.1", "mode": protocol_mode,
        "connections": {"Peer": {"port": port, "unitCode": peer_code}},
    }


def test_composite_combines_send_only_and_receive_only_members(manager, free_ports):
    port_send, port_recv = free_ports(2)
    composite = manager.create_composite("beacon", {
        "transport": _member("send_only", port_send, side="client"),
        "receive": _member("receive_only", port_recv, side="server"),
    })
    peer = manager.create("peer", {
        "protocol": "udp", "unitCode": TEXT_UNIT_CODE, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Beacon": {"port": port_recv, "unitCode": TEXT_UNIT_CODE}},
    })
    manager.start_all()

    unit, message = composite.receive_message(
        1, unit_name="Peer", timeout=3,
        trigger_function=lambda: peer.send_message(b"to-composite", 1),
    )
    assert bytes(message.data) == b"to-composite"


def test_composite_send_uses_the_send_capable_member(manager, free_ports):
    port_send, port_recv = free_ports(2)
    composite = manager.create_composite("beacon", {
        "transport": _member("send_only", port_send, side="client"),
        "receive": _member("receive_only", port_recv, side="server"),
    })
    peer = manager.create("peer", {
        "protocol": "udp", "unitCode": TEXT_UNIT_CODE, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Beacon": {"port": port_send, "unitCode": TEXT_UNIT_CODE}},
    })
    manager.start_all()

    unit, message = peer.receive_message(
        1, unitName="Beacon", timeout=3,
        trigger_function=lambda: composite.send_message(b"from-composite", 1, unit_name="Peer"),
    )
    assert bytes(message.data) == b"from-composite"


def test_composite_rejects_two_send_capable_members(manager, free_ports):
    port_a, port_b = free_ports(2)
    m1 = manager.create("m1", _member("send_only", port_a, 100))
    m2 = manager.create("m2", _member("send_only", port_b, 101))
    with pytest.raises(ValueError, match="send-capable"):
        CompositeUnit("bad", [m1, m2])


def test_composite_rejects_two_receive_capable_members(manager, free_ports):
    port_a, port_b = free_ports(2)
    m1 = manager.create("m1", _member("receive_only", port_a, 100, side="server"))
    m2 = manager.create("m2", _member("receive_only", port_b, 101, side="server"))
    with pytest.raises(ValueError, match="receive-capable"):
        CompositeUnit("bad", [m1, m2])


def test_composite_rejects_no_capable_members():
    with pytest.raises(ValueError, match="no member can send or receive"):
        CompositeUnit("bad", [])


def test_composite_receive_with_no_receiver_raises_runtimeerror(manager, free_port):
    composite = manager.create_composite("send-only-beacon", {
        "transport": _member("send_only", free_port, 100, side="client"),
    })
    with pytest.raises(RuntimeError, match="no receive-capable member"):
        composite.receive_message(1, unit_name="Peer", timeout=0.1)


def test_composite_send_with_no_sender_raises_runtimeerror(manager, free_port):
    composite = manager.create_composite("recv-only-beacon", {
        "receive": _member("receive_only", free_port, 100, side="server"),
    })
    with pytest.raises(RuntimeError, match="no send-capable member"):
        composite.send_message(b"x", 1, unit_name="Peer")


def test_composite_active_units_is_union_of_members(manager, free_ports):
    """`wait_for_connected_units` waits on EVERY member -- the send-only
    (client) member connects the instant it starts (its remote_addr is known
    upfront), but the receive-only (server) member only learns its peer from
    an actual inbound datagram, so both directions need real traffic before
    the composite as a whole reports "Peer" connected."""
    port_send, port_recv = free_ports(2)
    composite = manager.create_composite("beacon", {
        "transport": _member("send_only", port_send, side="client"),
        "receive": _member("receive_only", port_recv, side="server"),
    })
    peer_sender = manager.create("peer_sender", {
        "protocol": "udp", "unitCode": TEXT_UNIT_CODE, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Beacon": {"port": port_recv, "unitCode": TEXT_UNIT_CODE}},
    })
    manager.start_all()
    peer_sender.send_message(b"warm-up", 1)  # lets the receive member learn its peer

    assert composite.wait_for_connected_units("Peer", timeout=3) is True
    assert "Peer" in composite.active_units


def test_composite_close_tears_down_every_member_even_if_one_fails(manager, free_ports):
    port_send, port_recv = free_ports(2)
    composite = manager.create_composite("beacon", {
        "transport": _member("send_only", port_send, 100, side="client"),
        "receive": _member("receive_only", port_recv, 100, side="server"),
    })
    composite.start()

    failing_member = composite._members[0]
    original_close = failing_member.close
    def _boom(*a, **k):
        raise RuntimeError("simulated failure")
    failing_member.close = _boom

    with pytest.raises(RuntimeError, match="1 member"):
        composite.close()

    # The OTHER member must still have been closed despite the first raising.
    other = composite._members[1]
    assert other._started is False

    failing_member.close = original_close  # let the fixture's real teardown succeed
