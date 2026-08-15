"""
Per-link IRS end to end: the bug this whole mechanism exists to fix.

One process, one unit code, two peers, two structures files that both define
the SAME opcode under our own code with DIFFERENT layouts. Before per-link
structures, the second file's import erased the first and every message to both
peers encoded with whichever one happened to load last -- silently, with no
error anywhere.

The files are written to `tmp_path` rather than added under `IRS/Structures`
because the collision only matters when two genuinely independent files are in
play, and generating them here keeps the conflicting opcode out of every other
test's registry.
"""
from __future__ import annotations

import pytest

from IRS.irs_parser import IRSAmbiguousError, IRSDataError
from connections.config import ConnectionConfig
from tools.general import import_modules, resolve_module_name

OUR_CODE = 230              # this file's own range
PEER_A_CODE = 231
PEER_B_CODE = 232
SHARED_OPCODE = 940

#: Both files claim (OUR_CODE, SHARED_OPCODE) -- with different field layouts.
#: That is exactly the shape that used to silently overwrite.
_FILE = """
from IRS import *
from IRS.REGISTRY import register_message

class {cls}(Message):
{fields}

register_message(unitCode={ours}, opCode={opcode}, message={cls})

class {cls}Reply(Message):
    ack: int = Byte

register_message(unitCode={theirs}, opCode={opcode}, message={cls}Reply)
"""


@pytest.fixture
def link_structures(tmp_path):
    """Two independent structures files, returned as (raw path, namespace)."""
    written = []
    for cls, fields, theirs in (
        ("AlphaTrack", "    track_id: int = UInt16\n    heading: int = UInt16", PEER_A_CODE),
        ("BetaStatus", "    status: int = Byte", PEER_B_CODE),
    ):
        path = tmp_path / f"{cls.lower()}_link.py"
        path.write_text(_FILE.format(cls=cls, fields=fields, ours=OUR_CODE,
                                     theirs=theirs, opcode=SHARED_OPCODE))
        written.append((str(path), resolve_module_name(str(path))))
    return written


def _config(ports, structures_a=None, structures_b=None, connection_level=None):
    peer_a = {"port": ports[0], "unitCode": PEER_A_CODE}
    peer_b = {"port": ports[1], "unitCode": PEER_B_CODE}
    if structures_a:
        peer_a["Structures"] = [structures_a]
    if structures_b:
        peer_b["Structures"] = [structures_b]
    config = {
        "protocol": "udp", "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "unitCode": OUR_CODE,
        "connections": {"PeerA": peer_a, "PeerB": peer_b},
    }
    if connection_level:
        config["Structures"] = [connection_level]
    return config


# --------------------------------------------------------------------------- #
# The fix
# --------------------------------------------------------------------------- #
def test_two_peers_with_their_own_structures_encode_their_own_layouts(
    manager, free_ports, link_structures
):
    """Each link encodes with ITS file, even though both files register the
    same opcode under our single unit code."""
    (path_a, ns_a), (path_b, ns_b) = link_structures
    connection = manager.create("multi", _config(free_ports(2), path_a, path_b))

    assert connection.config.structures_for("PeerA") == (ns_a,)
    assert connection.config.structures_for("PeerB") == (ns_b,)

    # AlphaTrack is two UInt16s (4 bytes); BetaStatus is one Byte (1 byte).
    # Same opcode, same sender code -- only the destination differs.
    alpha = connection._encode(SHARED_OPCODE, {"track_id": 7, "heading": 270}, "PeerA")
    beta = connection._encode(SHARED_OPCODE, {"status": 3}, "PeerB")
    assert len(alpha) == 4, alpha
    assert len(beta) == 1, beta


def test_each_link_decodes_with_its_own_layout(manager, free_ports, link_structures):
    """The receive side of the same split: a peer's own code selects its file."""
    (path_a, _), (path_b, _) = link_structures
    connection = manager.create("multi", _config(free_ports(2), path_a, path_b))

    a_reply = connection._decode(PEER_A_CODE, SHARED_OPCODE, b"\x01", "PeerA")
    b_reply = connection._decode(PEER_B_CODE, SHARED_OPCODE, b"\x01", "PeerB")
    assert type(a_reply).__name__ == "AlphaTrackReply"
    assert type(b_reply).__name__ == "BetaStatusReply"


def test_manager_imports_every_per_unit_structures_list(manager, free_ports, link_structures):
    """`_import_config_libs` reads the union, not just a connection-level key --
    otherwise a per-unit config would import nothing at all."""
    (path_a, ns_a), (path_b, ns_b) = link_structures
    config = ConnectionConfig.from_json(_config(free_ports(2), path_a, path_b))
    assert config.all_structures_raw == (path_a, path_b)

    manager.create("multi", _config(free_ports(2), path_a, path_b))
    import sys
    assert ns_a in sys.modules and ns_b in sys.modules


# --------------------------------------------------------------------------- #
# What happens without per-link declarations
# --------------------------------------------------------------------------- #
def test_unscoped_encode_of_a_duplicated_route_raises_instead_of_guessing(
    manager, free_ports, link_structures
):
    """Drop the per-unit declarations and the route is genuinely ambiguous.
    It now fails loudly at the send rather than encoding the wrong layout."""
    (path_a, _), (path_b, _) = link_structures
    import_modules([path_a, path_b])        # both loaded, neither link declared

    connection = manager.create("multi", _config(free_ports(2)))
    with pytest.raises(IRSDataError) as excinfo:
        connection._encode(SHARED_OPCODE, {"track_id": 7, "heading": 270}, "PeerA")
    assert isinstance(excinfo.value.__cause__, IRSAmbiguousError)


def test_connection_level_structures_with_two_units_is_rejected(free_ports, link_structures):
    """A structures file describes ONE link, so it cannot be a connection-wide
    default once there is more than one link to apply it to."""
    (path_a, _), _ = link_structures
    with pytest.raises(ValueError, match="exactly one unit"):
        ConnectionConfig.from_json(_config(free_ports(2), connection_level=path_a))
