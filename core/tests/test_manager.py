"""
connections/manager.py -- ConnectionManager factory + lifecycle registry.
"""
import sys

import pytest

from core.connections.config import TransportProtocol
from core.connections.manager import ConnectionManager
from core.connections.udp import UdpConnection
from core.tests._messages import MESSAGES, TEXT_UNIT_CODE


def _udp_config(port, **overrides):
    cfg = {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": port, "unitCode": TEXT_UNIT_CODE, "Structures": [MESSAGES]}},
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
    if ConnectionManager._registry.get(TransportProtocol.DDS) is not None:
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
def test_create_composite_registers_only_the_composite(manager, free_ports):
    port_send, port_recv = free_ports(2)
    composite = manager.create_composite("beacon", {
        "transport": {
            "protocol": "udp", "unitCode": 100, "side": "client",
            "ip": "127.0.0.1", "local_ip": "127.0.0.1", "mode": "send_only",
            "connections": {"Peer": {"port": port_send, "unitCode": TEXT_UNIT_CODE, "Structures": [MESSAGES]}},
        },
        "receive": {
            "protocol": "udp", "unitCode": 100, "side": "server",
            "ip": "127.0.0.1", "local_ip": "127.0.0.1", "mode": "receive_only",
            "connections": {"Peer": {"port": port_recv, "unitCode": TEXT_UNIT_CODE, "Structures": [MESSAGES]}},
        },
    })
    assert manager.get("beacon") is composite
    assert list(manager._connections) == ["beacon"], "members belong to the composite"


def test_create_refuses_a_taken_name(manager, free_ports):
    port_a, port_b = free_ports(2)
    manager.create("c", _udp_config(port_a))
    with pytest.raises(ValueError, match="already registered"):
        manager.create("c", _udp_config(port_b))


def test_a_failed_create_composite_registers_nothing(manager, free_ports):
    port_a, port_b = free_ports(2)
    duplex = {"protocol": "udp", "unitCode": 100, "side": "server", "ip": "127.0.0.1",
              "local_ip": "127.0.0.1"}
    with pytest.raises(ValueError, match="more than one send-capable"):
        manager.create_composite("beacon", {
            "a": {**duplex, "connections": {"Peer": {"port": port_a, "unitCode": TEXT_UNIT_CODE, "Structures": [MESSAGES]}}},
            "b": {**duplex, "connections": {"Peer": {"port": port_b, "unitCode": TEXT_UNIT_CODE, "Structures": [MESSAGES]}}},
        })
    assert manager._connections == {}


# --------------------------------------------------------------------------- #
# "Structures": a path, or a module name imported as-is
# --------------------------------------------------------------------------- #
#: The structures modules this repo ships inside a real package, so they can be
#: named the dotted way. Nothing is prefixed onto an entry -- a dotted entry IS
#: the module name.
TEST_MESSAGES = "core.IRS.Structures.Test.test_messages"


def _track_config(port, structures):
    """We are test_messages' Client (21), talking to its Server (22)."""
    return {
        "protocol": "udp", "unitCode": 21, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Server": {"port": port, "unitCode": 22, "Structures": structures}},
    }


def test_a_link_without_structures_is_refused(manager, free_port):
    config = _track_config(free_port, [])
    with pytest.raises(ValueError, match="has no 'Structures'"):
        manager.create("c", config)
    assert manager._connections == {}


def test_a_dotted_structures_entry_is_imported_verbatim(manager, free_port):
    """No prefix, no rewriting, no folder it has to live in.

    Every entry -- short or fully qualified, slashes or dots -- used to be
    rewritten to sit under one blessed structures package. That made the name a
    config declared depend on a folder inside the app, and the failure when that
    folder moved was `No module named 'core.IRS.Structures'` for a module that
    was importable all along. A dotted entry is now an ordinary import.
    """
    sys.modules.pop(TEST_MESSAGES, None)
    manager.create("c", _track_config(free_port, [TEST_MESSAGES]))
    assert TEST_MESSAGES in sys.modules


def test_a_dotted_entry_that_is_not_importable_names_the_entry(manager, free_port):
    """The error a user can act on: what they wrote, not a package name
    invented three layers down."""
    with pytest.raises(ModuleNotFoundError, match=r"Nope\.messages"):
        manager.create("c", _track_config(free_port, ["Nope.messages"]))


def test_the_link_interface_is_ready_when_create_returns(manager, free_port):
    """So a config can never come up unable to receive messages it declares
    layouts for."""
    from core.IRS.Structures.Test.test_messages import TRACK_OPCODE, TrackAck, TrackReport

    connection = manager.create("c", _track_config(free_port, [TEST_MESSAGES]))
    interface = connection.interface_for("Server")
    assert interface.message_class(21, TRACK_OPCODE) is TrackReport
    assert interface.message_class(22, TRACK_OPCODE) is TrackAck


# --------------------------------------------------------------------------- #
# Module naming: what a config declares must equal what gets imported
# --------------------------------------------------------------------------- #
def test_import_modules_loads_the_module_resolve_module_name_predicts():
    """Config resolution and the actual import go through one function, so a
    link's declared namespace is always the module it really loaded."""
    import importlib
    from pathlib import Path

    from core.tools.general import import_modules, resolve_module_name

    module_file = str(Path(importlib.import_module(TEST_MESSAGES).__file__).resolve())
    spellings = [TEST_MESSAGES, module_file]
    names = [module.__name__ for module in import_modules(spellings)]
    assert names == [resolve_module_name(s) for s in spellings]
    # A dotted entry keeps its own name; a path gets a synthetic one derived
    # from the path. Two spellings of one file are two modules -- on purpose:
    # collapsing them is what used to require a folder to be special.
    assert names[0] == TEST_MESSAGES


def test_a_picked_path_is_loaded_from_that_path_wherever_it_lives():
    """The file named is the file that runs -- location decides nothing.

    The namespace is derived from the path itself, under a synthetic root that
    is nothing on disk and is never searched for, so a file kept anywhere loads
    without this repo keeping any folder alive for it.
    """
    from pathlib import Path

    from core.IRS.irs_parser import Interface
    from core.tools.general import import_modules, resolve_module_name
    import core.IRS.Structures.Test.test_messages as module
    from core.IRS.Structures.Test.test_messages import TRACK_OPCODE

    by_path = str(Path(module.__file__).resolve())
    name = resolve_module_name(by_path)
    # <root>.<real folder chain>.<stem> -- nothing in it names a real package.
    assert name.startswith("irs_structures."), name
    assert name.endswith(".test_messages"), name
    [loaded] = import_modules([by_path])
    assert loaded.__name__ == name
    interface = Interface.from_module(loaded)
    assert interface.message_class(21, TRACK_OPCODE).__name__ == "TrackReport"


def test_a_structures_path_that_does_not_exist_names_the_missing_file(tmp_path):
    """The likeliest bad entry is a config written on another machine: a
    structures path is absolute and absolute paths are not portable. Absent must
    read as absent, not as a file that failed to import."""
    from core.tools.general import import_modules

    missing = tmp_path / "gone" / "messages.py"
    with pytest.raises(ImportError, match="structures file does not exist"):
        import_modules([str(missing)])


def test_a_file_with_no_definition_is_not_a_structures_file(tmp_path):
    from core.tools.general import import_modules

    path = tmp_path / "empty_messages.py"
    path.write_text("from IRS import *\nUnit = 1\n")
    with pytest.raises(ImportError, match="defines no @Definition message"):
        import_modules([str(path)])


def test_same_named_files_in_different_directories_do_not_clobber(tmp_path):
    """`sys.modules[path.stem]` used to collapse both onto one entry, so the
    second import silently erased the first."""
    from core.IRS.irs_parser import Interface
    from core.tools.general import import_modules

    body = (
        "from IRS import *\n"
        "@Definition(unitCode=24{n}, opCode=950)\n"
        "class M{n}(Message):\n"
        "    v: int = UInt16\n"
    )
    paths = []
    for n in (1, 2):
        directory = tmp_path / f"dir{n}"
        directory.mkdir()
        target = directory / "messages.py"      # same basename, different dir
        target.write_text(body.format(n=n))
        paths.append(str(target))

    first, second = import_modules(paths)
    assert first.__name__ != second.__name__
    assert Interface.from_module(first).message_class(241, 950).__name__ == "M1"
    assert Interface.from_module(second).message_class(242, 950).__name__ == "M2"
