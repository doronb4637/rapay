"""
connections/handlers.py -- `route`/`UnitHandler` class-definition-time route
collection, and `install_handler`'s wiring into `Connection.handle_on_receive`.
"""
import pytest

from core.connections.handlers import UnitHandler, install_handler, route
from core.connections.manager import ConnectionManager
from core.tests._messages import TEXT_UNIT_CODE, TEXT_UNIT_CODE_2


# --------------------------------------------------------------------------- #
# Pure class-definition-time behaviour -- no connection, no event loop.
# --------------------------------------------------------------------------- #
def test_route_map_built_at_class_definition_time():
    class Handler(UnitHandler):
        unitCode = 1

        @route(opCode=10)
        def a(self, message):
            pass

        @route(opCode=11)
        def b(self, message):
            pass

    assert Handler._routes == {10: "a", 11: "b"}


def test_unitCode_is_required_on_every_subclass():
    with pytest.raises(TypeError, match="unitCode"):
        class Handler(UnitHandler):
            @route(opCode=1)
            def a(self, message):
                pass


def test_unitCode_zero_is_a_valid_code():
    """0 is falsy but a legitimate unit code -- must not be treated as
    'unset'."""
    class Handler(UnitHandler):
        unitCode = 0

    assert Handler.unitCode == 0


def test_duplicate_opcode_in_one_class_raises_at_definition_time():
    with pytest.raises(TypeError, match="opCode"):
        class Handler(UnitHandler):
            unitCode = 1

            @route(opCode=5)
            def m1(self, message):
                pass

            @route(opCode=5)
            def m2(self, message):
                pass


def test_untagged_method_is_not_routed():
    class Handler(UnitHandler):
        unitCode = 1

        def plain(self, message):
            pass

        @route(opCode=1)
        def tagged(self, message):
            pass

    assert Handler._routes == {1: "tagged"}


def test_subclass_overriding_and_retagging_replaces_the_route():
    class Base(UnitHandler):
        unitCode = 1

        @route(opCode=10)
        def handle(self, message):
            return "base"

    class Sub(Base):
        unitCode = 1

        @route(opCode=20)
        def handle(self, message):
            return "sub"

    # Only the override's opcode is live -- the base's opcode 10 does not
    # also survive as a second route to the same (now-overridden) method.
    assert Sub._routes == {20: "handle"}


def test_subclass_override_without_redecorating_unroutes_it():
    class Base(UnitHandler):
        unitCode = 1

        @route(opCode=10)
        def handle(self, message):
            return "base"

    class Sub(Base):
        unitCode = 1

        def handle(self, message):  # no @route -- deliberately un-routed
            return "sub"

    assert Sub._routes == {}


def test_subclass_inherits_untouched_base_routes():
    class Base(UnitHandler):
        unitCode = 1

        @route(opCode=10)
        def handle(self, message):
            pass

    class Sub(Base):
        unitCode = 1

    assert Sub._routes == {10: "handle"}


def test_default_init_stores_unit_connection():
    class Handler(UnitHandler):
        unitCode = 1

    sentinel = object()
    handler = Handler(sentinel)
    assert handler.unitConnection is sentinel


# --------------------------------------------------------------------------- #
# install_handler / ConnectionManager.create(handler_class=...) wiring
# --------------------------------------------------------------------------- #
def test_install_handler_registers_every_route_as_a_callback(manager, free_port):
    received = []

    class Handler(UnitHandler):
        unitCode = TEXT_UNIT_CODE

        @route(opCode=1)
        def on_one(self, message):
            received.append(("one", bytes(message.data)))

        @route(opCode=2)
        def on_two(self, message):
            received.append(("two", bytes(message.data)))

    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    }, handler_class=Handler)
    client = manager.create("client", {
        "protocol": "udp", "unitCode": TEXT_UNIT_CODE, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": 100}},
    })
    manager.start_all()

    client.send_message(b"a", 1)
    client.send_message(b"b", 2)

    import time
    deadline = time.monotonic() + 3
    while len(received) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)

    assert sorted(received) == [("one", b"a"), ("two", b"b")]


def test_install_handler_unknown_unit_code_raises_and_leaves_manager_clean(manager, free_port):
    class Orphan(UnitHandler):
        unitCode = 0xFE  # no configured connection uses this code

        @route(opCode=1)
        def handle(self, message):
            pass

    with pytest.raises(ValueError, match="0xfe|254"):
        manager.create("server", {
            "protocol": "udp", "unitCode": 100, "side": "server",
            "ip": "127.0.0.1", "local_ip": "127.0.0.1",
            "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
        }, handler_class=Orphan)

    with pytest.raises(KeyError):
        manager.get("server")


def test_install_handler_route_occupies_slot_like_ordinary_callback(manager, free_port):
    class Handler(UnitHandler):
        unitCode = TEXT_UNIT_CODE

        @route(opCode=1)
        def handle(self, message):
            pass

    server = manager.create("server", {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    }, handler_class=Handler)
    server.start()

    with pytest.raises(RuntimeError):
        server.receive_message(1, unit_name="Peer", timeout=0.5)

    # A different opcode on the same connection is untouched.
    with pytest.raises(TimeoutError):
        server.receive_message(2, unit_name="Peer", timeout=0.3)


def test_install_handler_on_composite(manager, free_ports):
    """`CompositeUnit` has no `.config` of its own -- install_handler must
    reach through its receive-capable member."""
    port_send, port_recv = free_ports(2)
    received = []

    class Handler(UnitHandler):
        unitCode = TEXT_UNIT_CODE_2

        @route(opCode=1)
        def handle(self, message):
            received.append(bytes(message.data))

    composite = manager.create_composite("composite", {
        "transport": {
            "protocol": "udp", "unitCode": 100, "side": "client",
            "ip": "127.0.0.1", "local_ip": "127.0.0.1", "mode": "send_only",
            "connections": {"Peer": {"port": port_send, "unitCode": TEXT_UNIT_CODE_2}},
        },
        "receive": {
            "protocol": "udp", "unitCode": 100, "side": "server",
            "ip": "127.0.0.1", "local_ip": "127.0.0.1", "mode": "receive_only",
            "connections": {"Peer": {"port": port_recv, "unitCode": TEXT_UNIT_CODE_2}},
        },
    }, handler_class=Handler)
    peer = manager.create("peer", {
        "protocol": "udp", "unitCode": TEXT_UNIT_CODE_2, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Composite": {"port": port_recv, "unitCode": 100}},
    })
    manager.start_all()

    peer.send_message(b"hi", 1)

    import time
    deadline = time.monotonic() + 3
    while not received and time.monotonic() < deadline:
        time.sleep(0.02)

    assert received == [b"hi"]
