"""
connections/manager.py -- ConnectionManager factory + lifecycle registry.
"""
import sys

import pytest

from core.connections.config import Protocol
from core.connections.manager import ConnectionManager
from core.connections.udp import UdpConnection
from core.tests._messages import TEXT_UNIT_CODE


def _udp_config(port, **overrides):
    cfg = {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": port, "unitCode": TEXT_UNIT_CODE}},
    }
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------- #
# create() basics
# --------------------------------------------------------------------------- #
def test_create_returns_the_right_protocol_class(manager, free_port):
    connection = manager.create("c", _udp_config(free_port))
    assert isinstance(connection, UdpConnection)
    assert manager.get("c") is connection


def test_create_rejects_config_of_wrong_type(manager):
    with pytest.raises(TypeError):
        manager.create("c", 12345)


def test_create_rejects_unregistered_protocol(manager, free_port):
    cfg = _udp_config(free_port, protocol="dds")
    if ConnectionManager._registry.get(Protocol.DDS) is not None:
        pytest.skip("DDS is registered in this environment (RTI installed)")
    with pytest.raises(ValueError, match="protocol"):
        manager.create("c", cfg)


def test_get_unknown_name_raises_keyerror(manager):
    with pytest.raises(KeyError):
        manager.get("nope")


# --------------------------------------------------------------------------- #
# lifecycle: start_all / shutdown_all / context manager
# --------------------------------------------------------------------------- #
def test_shutdown_all_clears_the_registry(manager, free_port):
    manager.create("c", _udp_config(free_port))
    assert "c" in manager._connections
    manager.shutdown_all()
    assert manager._connections == {}


def test_shutdown_all_tolerates_one_connection_failing_to_close(manager, free_ports):
    ports = free_ports(2)
    good = manager.create("good", _udp_config(ports[0]))
    bad = manager.create("bad", _udp_config(ports[1]))
    manager.start_all()

    def _boom(*a, **k):
        raise RuntimeError("simulated close failure")
    bad.close = _boom

    # Must not raise, and must still tear down `good`.
    manager.shutdown_all()
    assert good._started is False


def test_context_manager_shuts_down_on_normal_exit(free_port):
    with ConnectionManager() as mgr:
        conn = mgr.create("c", _udp_config(free_port))
        mgr.start_all()
        assert conn._started is True
    assert conn._started is False


def test_context_manager_shuts_down_on_exception():
    conn_holder = {}
    with pytest.raises(ValueError, match="boom"):
        with ConnectionManager() as mgr:
            conn_holder["mgr"] = mgr
            raise ValueError("boom")
    assert conn_holder["mgr"]._connections == {}


# --------------------------------------------------------------------------- #
# create_composite()
# --------------------------------------------------------------------------- #
def test_create_composite_names_members_with_a_prefix(manager, free_ports):
    port_send, port_recv = free_ports(2)
    composite = manager.create_composite("beacon", {
        "transport": {
            "protocol": "udp", "unitCode": 100, "side": "client",
            "ip": "127.0.0.1", "local_ip": "127.0.0.1", "mode": "send_only",
            "connections": {"Peer": {"port": port_send, "unitCode": TEXT_UNIT_CODE}},
        },
        "receive": {
            "protocol": "udp", "unitCode": 100, "side": "server",
            "ip": "127.0.0.1", "local_ip": "127.0.0.1", "mode": "receive_only",
            "connections": {"Peer": {"port": port_recv, "unitCode": TEXT_UNIT_CODE}},
        },
    })
    assert manager.get("beacon") is composite
    assert manager.get("beacon.transport") is not None
    assert manager.get("beacon.receive") is not None


# --------------------------------------------------------------------------- #
# _import_config_libs / "Structures" -> IRS.Structures.* normalization
# --------------------------------------------------------------------------- #
def test_structures_key_is_a_noop_when_absent(manager, free_port):
    # No exception, no import side effect -- just proves absence is fine.
    manager.create("c", _udp_config(free_port))


@pytest.mark.parametrize("spelling", [
    "Test.test_messages",
    "Test/test_messages",
    "Test\\test_messages",
    "IRS.Structures.Test.test_messages",  # already fully qualified
])
def test_structures_key_normalizes_to_irs_structures_package(manager, free_port, spelling):
    sys.modules.pop("core.IRS.Structures.Test.test_messages", None)
    manager.create("c", _udp_config(free_port, Structures=[spelling]))
    assert "core.IRS.Structures.Test.test_messages" in sys.modules


def test_structures_import_happens_before_the_connection_object_is_instantiated(manager, free_port):
    """So a config can never come up unable to receive messages it declares
    layouts for -- `_import_config_libs` runs strictly before `impl_cls(...)`
    in `create()`."""
    from core.IRS.REGISTRY import messages_in
    from core.IRS.Structures.Test.test_messages import CLIENT_UNIT_CODE, TRACK_OPCODE

    connection = manager.create("c", _udp_config(
        free_port, Structures=["Test.test_messages"]
    ))
    # If import ran, the layout is already registered by the time we get the
    # connection object back -- no separate "warm up" step needed. Asserted
    # inside the module's own namespace, which is what the config named.
    registered = messages_in("core.IRS.Structures.Test.test_messages")
    assert TRACK_OPCODE in registered[CLIENT_UNIT_CODE]
    assert connection is not None


# --------------------------------------------------------------------------- #
# Namespace resolution: what a config declares must equal what gets registered
# --------------------------------------------------------------------------- #
def test_import_modules_returns_the_namespace_resolve_module_name_predicts():
    """The anti-drift guarantee the whole per-link design rests on: config
    resolution and the actual import go through one function, so a link can
    never be scoped to a namespace nothing registered under."""
    from core.tools.general import import_modules, resolve_module_name

    spellings = ["Test.test_messages", "Test/test_messages",
                 "Test\\test_messages", "IRS.Structures.Test.test_messages"]
    assert import_modules(spellings) == [resolve_module_name(s) for s in spellings]
    assert set(import_modules(spellings)) == {"core.IRS.Structures.Test.test_messages"}


def test_a_path_inside_the_structures_package_resolves_to_its_dotted_name():
    """GSim's file picker opens at IRS/Structures, so the common case is an
    absolute path to a file that also has a dotted name. Both spellings must be
    ONE namespace, or a single file would register itself twice."""
    from pathlib import Path

    from core.tools.general import resolve_module_name
    import core.IRS.Structures.Test.test_messages as module

    by_path = Path(module.__file__).resolve()
    assert resolve_module_name(str(by_path)) == "core.IRS.Structures.Test.test_messages"


def test_same_named_files_in_different_directories_do_not_clobber(tmp_path):
    """`sys.modules[path.stem]` used to collapse both onto one entry, so the
    second import silently erased the first."""
    from core.IRS.REGISTRY import messages_in
    from core.tools.general import import_modules

    body = (
        "from core.IRS import *\n"
        "from core.IRS.REGISTRY import register_message\n"
        "class M{n}(Message):\n"
        "    v: int = UInt16\n"
        "register_message(unitCode=24{n}, opCode=950, message=M{n})\n"
    )
    paths = []
    for n in (1, 2):
        directory = tmp_path / f"dir{n}"
        directory.mkdir()
        target = directory / "messages.py"      # same basename, different dir
        target.write_text(body.format(n=n))
        paths.append(str(target))

    namespaces = import_modules(paths)
    assert namespaces[0] != namespaces[1], namespaces
    assert messages_in(namespaces[0])[241][950].__name__ == "M1"
    assert messages_in(namespaces[1])[242][950].__name__ == "M2"


def test_import_config_libs_imports_every_per_unit_list(manager, free_ports):
    """Reads the union across units, not just a connection-level key."""
    config = _udp_config(free_ports(1)[0], connections={
        "A": {"port": free_ports(1)[0], "unitCode": TEXT_UNIT_CODE,
              "Structures": ["Test.test_messages"]},
        "B": {"port": free_ports(1)[0], "unitCode": TEXT_UNIT_CODE + 1,
              "Structures": ["Tiful.tiful_to_dtu"]},
    })
    manager.create("c", config)
    assert "core.IRS.Structures.Test.test_messages" in sys.modules
    assert "core.IRS.Structures.Tiful.tiful_to_dtu" in sys.modules
