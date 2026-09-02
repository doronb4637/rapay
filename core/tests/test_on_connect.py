"""
connections/base.py -- `handle_on_connect` / `stop_on_connect`: the connect-time
counterpart to `handle_on_receive`, keyed by unit name alone since a connect
event carries no opcode.

Characterization coverage: none of this, nor the `@on_connect` decorator that
installs through it, was exercised by the suite.
"""
import threading

import pytest

from core.connections.handlers import UnitHandler, on_connect
from core.tests._messages import TEXT_UNIT_CODE

#: A UDP client knows its peer address from `_do_start`, so it reports the unit
#: connected the moment `start()` returns -- no handshake to wait on.
CLIENT_SPEC = {
    "protocol": "udp", "unitCode": 101, "side": "client",
    "ip": "127.0.0.1", "local_ip": "127.0.0.1",
}


def _client(manager, port, name="client", **overrides):
    spec = dict(CLIENT_SPEC, **overrides)
    spec["connections"] = {"Peer": {"port": port, "unitCode": TEXT_UNIT_CODE}}
    return manager.create(name, spec)


class Recorder:
    """Records the unit names an on-connect callback was invoked with, from
    the executor thread it runs on."""

    def __init__(self) -> None:
        self.fired = threading.Event()
        self.units: list[str] = []

    def __call__(self, unit_name: str) -> None:
        self.units.append(unit_name)
        self.fired.set()


def test_handle_on_connect_fires_with_the_unit_name(manager, free_port):
    client = _client(manager, free_port)
    recorder = Recorder()
    client.handle_on_connect(recorder, unit_name="Peer")

    client.start()

    assert recorder.fired.wait(3) is True
    assert recorder.units == ["Peer"]


def test_a_unit_may_only_carry_one_on_connect_callback(manager, free_port):
    client = _client(manager, free_port)
    client.handle_on_connect(lambda unit: None, unit_name="Peer")

    with pytest.raises(RuntimeError, match="already has an on-connect callback"):
        client.handle_on_connect(lambda unit: None, unit_name="Peer")


def test_stop_on_connect_reports_whether_there_was_one_to_remove(manager, free_port):
    client = _client(manager, free_port)
    client.handle_on_connect(lambda unit: None, unit_name="Peer")

    assert client.stop_on_connect(unit_name="Peer") is True
    assert client.stop_on_connect(unit_name="Peer") is False


def test_the_slot_is_reusable_after_stop_on_connect(manager, free_port):
    client = _client(manager, free_port)
    client.handle_on_connect(lambda unit: None, unit_name="Peer")
    client.stop_on_connect(unit_name="Peer")

    recorder = Recorder()
    client.handle_on_connect(recorder, unit_name="Peer")
    client.start()

    assert recorder.fired.wait(3) is True


def test_registering_on_an_already_connected_unit_does_not_fire_retroactively(manager, free_port):
    """Only the NEXT transition into connected fires. Callers that care about
    a unit already up read `active_units` themselves."""
    client = _client(manager, free_port)
    client.start()
    assert client.wait_for_connected_units("Peer", timeout=3) is True

    recorder = Recorder()
    client.handle_on_connect(recorder, unit_name="Peer")

    assert recorder.fired.wait(0.5) is False
    assert recorder.units == []


def test_a_non_callable_callback_is_rejected(manager, free_port):
    client = _client(manager, free_port)
    with pytest.raises(TypeError, match="must be callable"):
        client.handle_on_connect("not-a-callback", unit_name="Peer")


def test_unknown_unit_name_is_rejected(manager, free_port):
    client = _client(manager, free_port)
    with pytest.raises(ValueError, match="Unknown unit"):
        client.handle_on_connect(lambda unit: None, unit_name="Nope")


# --------------------------------------------------------------------------- #
# The declarative form: @on_connect on a UnitHandler installs nothing more than
# an ordinary handle_on_connect callback.
# --------------------------------------------------------------------------- #
GREETED = threading.Event()
GREETED_UNITS: list[str] = []


class GreetingHandler(UnitHandler):
    unitCode = TEXT_UNIT_CODE

    @on_connect
    def greet(self, unit_name: str) -> None:
        GREETED_UNITS.append(unit_name)
        GREETED.set()


def test_on_connect_decorator_installs_through_handle_on_connect(manager, free_port):
    GREETED.clear()
    GREETED_UNITS.clear()

    spec = dict(CLIENT_SPEC)
    spec["connections"] = {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}}
    client = manager.create("client", spec, handler_class=GreetingHandler)

    # Installed by create(), so the slot is already claimed before start().
    with pytest.raises(RuntimeError, match="already has an on-connect callback"):
        client.handle_on_connect(lambda unit: None, unit_name="Peer")

    client.start()
    assert GREETED.wait(3) is True
    assert GREETED_UNITS == ["Peer"]


def test_two_on_connect_methods_on_one_handler_is_a_definition_time_error():
    with pytest.raises(TypeError, match="@on_connect"):
        class TwoGreetings(UnitHandler):
            unitCode = TEXT_UNIT_CODE

            @on_connect
            def greet(self, unit_name: str) -> None:
                ...

            @on_connect
            def also_greet(self, unit_name: str) -> None:
                ...
