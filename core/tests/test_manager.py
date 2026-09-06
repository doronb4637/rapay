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
# _import_config_libs / "Structures": a path, or a module name imported as-is
# --------------------------------------------------------------------------- #
#: The structures modules this repo ships inside a real package, so they can be
#: named the dotted way. Nothing is prefixed onto an entry any more -- a dotted
#: entry IS the module name.
TEST_MESSAGES = "core.IRS.Structures.Test.test_messages"
TIFUL_MESSAGES = "core.IRS.Structures.Tiful.tiful_to_dtu"


def test_structures_key_is_a_noop_when_absent(manager, free_port):
    # No exception, no import side effect -- just proves absence is fine.
    manager.create("c", _udp_config(free_port))


def test_a_dotted_structures_entry_is_imported_verbatim(manager, free_port):
    """No prefix, no rewriting, no folder it has to live in.

    Every entry -- short or fully qualified, slashes or dots -- used to be
    rewritten to sit under one blessed structures package. That made the name a
    config declared depend on a folder inside the app, and the failure when that
    folder moved was `No module named 'core.IRS.Structures'` for a module that
    was importable all along. A dotted entry is now an ordinary import.
    """
    sys.modules.pop(TEST_MESSAGES, None)
    manager.create("c", _udp_config(free_port, Structures=[TEST_MESSAGES]))
    assert TEST_MESSAGES in sys.modules


def test_a_dotted_entry_that_is_not_importable_names_the_entry(manager, free_port):
    """The error a user can act on: what they wrote, not a package name
    invented three layers down."""
    with pytest.raises(ModuleNotFoundError, match=r"Nope\.messages"):
        manager.create("c", _udp_config(free_port, Structures=["Nope.messages"]))


def test_structures_import_happens_before_the_connection_object_is_instantiated(manager, free_port):
    """So a config can never come up unable to receive messages it declares
    layouts for -- `_import_config_libs` runs strictly before `impl_cls(...)`
    in `create()`."""
    from core.IRS.REGISTRY import get_specification
    from core.IRS.Structures.Test.test_messages import CLIENT_UNIT_CODE, TRACK_OPCODE

    connection = manager.create("c", _udp_config(
        free_port, Structures=[TEST_MESSAGES]
    ))
    # If import ran, the layout is already registered by the time we get the
    # connection object back -- no separate "warm up" step needed. Asserted
    # inside the module's own namespace, which is what the config named.
    registered = get_specification(TEST_MESSAGES)
    assert TRACK_OPCODE in registered[CLIENT_UNIT_CODE]
    assert connection is not None


# --------------------------------------------------------------------------- #
# Namespace resolution: what a config declares must equal what gets registered
# --------------------------------------------------------------------------- #
def test_import_modules_returns_the_namespace_resolve_module_name_predicts():
    """The anti-drift guarantee the whole per-link design rests on: config
    resolution and the actual import go through one function, so a link can
    never be scoped to a namespace nothing registered under."""
    import importlib
    from pathlib import Path

    from core.tools.general import import_modules, resolve_module_name

    module_file = str(Path(importlib.import_module(TEST_MESSAGES).__file__).resolve())
    spellings = [TEST_MESSAGES, module_file]
    assert import_modules(spellings) == [resolve_module_name(s) for s in spellings]
    # A dotted entry keeps its own name; a path gets a synthetic one derived
    # from the path. Two spellings of one file are two namespaces -- on purpose:
    # collapsing them is what used to require a folder to be special.
    assert import_modules(spellings)[0] == TEST_MESSAGES


def test_a_picked_path_is_loaded_from_that_path_wherever_it_lives():
    """The file named is the file that runs -- location decides nothing.

    Resolution used to branch on WHERE a file sat, and then -- once that was
    fixed -- still NAMED every picked file as a member of a structures package
    inside the app. Both tie a file the user keeps wherever they like to a
    folder this repo has to keep alive, and both fail the same way the moment
    it moves: `No module named '<that package>'` for a file sitting right there
    on disk. The namespace is derived from the path itself now, under a
    synthetic root that is nothing on disk and is never searched for.
    """
    from pathlib import Path

    from core.IRS.REGISTRY import get_specification
    from core.tools.general import import_modules, resolve_module_name
    import core.IRS.Structures.Test.test_messages as module
    from core.IRS.Structures.Test.test_messages import CLIENT_UNIT_CODE, TRACK_OPCODE

    by_path = str(Path(module.__file__).resolve())
    name = resolve_module_name(by_path)
    # <root>.<real folder chain>.<stem> -- nothing in it names a real package.
    assert name.startswith("irs_structures."), name
    assert name.endswith(".test_messages"), name
    # ...and it really is imported from the file, registering under that name.
    assert import_modules([by_path]) == [name]
    assert TRACK_OPCODE in get_specification(name)[CLIENT_UNIT_CODE]


def test_a_structures_path_that_does_not_exist_names_the_missing_file(tmp_path):
    """The likeliest bad entry is a config written on another machine: a
    structures path is absolute and absolute paths are not portable. Absent must
    read as absent, not as a file that failed to import."""
    import pytest

    from core.tools.general import import_modules

    missing = tmp_path / "gone" / "messages.py"
    with pytest.raises(ImportError, match="structures file does not exist"):
        import_modules([str(missing)])


def test_same_named_files_in_different_directories_do_not_clobber(tmp_path):
    """`sys.modules[path.stem]` used to collapse both onto one entry, so the
    second import silently erased the first."""
    from core.IRS.REGISTRY import get_specification
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
    assert get_specification(namespaces[0])[241][950].__name__ == "M1"
    assert get_specification(namespaces[1])[242][950].__name__ == "M2"


def test_import_config_libs_imports_every_per_unit_list(manager, free_ports):
    """Reads the union across units, not just a connection-level key."""
    config = _udp_config(free_ports(1)[0], connections={
        "A": {"port": free_ports(1)[0], "unitCode": TEXT_UNIT_CODE,
              "Structures": [TEST_MESSAGES]},
        "B": {"port": free_ports(1)[0], "unitCode": TEXT_UNIT_CODE + 1,
              "Structures": [TIFUL_MESSAGES]},
    })
    manager.create("c", config)
    assert TEST_MESSAGES in sys.modules
    assert TIFUL_MESSAGES in sys.modules
