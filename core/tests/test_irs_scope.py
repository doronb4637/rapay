"""
Per-link IRS end to end: every connection holds its own interface per link.

One process, one unit code, two peers, two structures files that both define
the SAME opcode under our own code with DIFFERENT layouts. With a global
registry the second file erased the first; now each link only ever sees its
own file, and the config's unit names are checked against it.

The files are written to `tmp_path` rather than added under `IRS/Structures`
because the collision only matters when two genuinely independent files are in
play.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import core.IRS
from core.IRS.irs_parser import IRSAmbiguousError, IRSNotFoundError
from core.connections.config import ConnectionConfig
from core.tools.general import resolve_module_name

OUR_CODE = 230
PEER_A_CODE = 231
PEER_B_CODE = 232
SHARED_OPCODE = 940

#: Both files define (OUR_CODE, SHARED_OPCODE) -- with different field layouts.
_FILE = """
from IRS import *

Us = {ours}
{peer} = {theirs}

@Definition(unitCode=Us, opCode={opcode})
class {cls}(Message):
{fields}

@Definition(unitCode={peer}, opCode={opcode})
class {cls}Reply(Message):
    ack: int = Byte
"""


@pytest.fixture
def link_structures(tmp_path):
    """Two independent structures files, returned as (raw path, namespace)."""
    written = []
    for cls, fields, peer, theirs in (
        ("AlphaTrack", "    track_id: int = UInt16\n    heading: int = UInt16", "PeerA", PEER_A_CODE),
        ("BetaStatus", "    status: int = Byte", "PeerB", PEER_B_CODE),
    ):
        path = tmp_path / f"{cls.lower()}_link.py"
        path.write_text(_FILE.format(cls=cls, fields=fields, ours=OUR_CODE, peer=peer,
                                     theirs=theirs, opcode=SHARED_OPCODE))
        written.append((str(path), resolve_module_name(str(path))))
    return written


def _config(ports, structures_a=None, structures_b=None, connection_level=None,
            own_code=OUR_CODE, code_a=PEER_A_CODE, name_a="PeerA"):
    peer_a = {"port": ports[0], "unitCode": code_a}
    peer_b = {"port": ports[1], "unitCode": PEER_B_CODE}
    if structures_a:
        peer_a["Structures"] = structures_a if isinstance(structures_a, list) else [structures_a]
    if structures_b:
        peer_b["Structures"] = [structures_b]
    config = {
        "protocol": "udp", "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "unitCode": own_code,
        "connections": {name_a: peer_a, "PeerB": peer_b},
    }
    if connection_level:
        config["Structures"] = [connection_level]
    return config


# --------------------------------------------------------------------------- #
# Each link sees only its own file
# --------------------------------------------------------------------------- #
def test_two_peers_with_their_own_structures_send_their_own_layouts(
    manager, free_ports, link_structures
):
    """Same opcode, same sender code -- only the destination's file differs."""
    (path_a, ns_a), (path_b, ns_b) = link_structures
    connection = manager.create("multi", _config(free_ports(2), path_a, path_b))

    alpha_class = connection.interface_for("PeerA").message_class(OUR_CODE, SHARED_OPCODE)
    beta_class = connection.interface_for("PeerB").message_class(OUR_CODE, SHARED_OPCODE)
    assert (alpha_class.__name__, alpha_class.__module__) == ("AlphaTrack", ns_a)
    assert (beta_class.__name__, beta_class.__module__) == ("BetaStatus", ns_b)

    # AlphaTrack is two UInt16s (4 bytes); BetaStatus is one Byte (1 byte).
    assert len(connection._encode(SHARED_OPCODE, alpha_class(track_id=7, heading=270), "PeerA")) == 4
    assert len(connection._encode(SHARED_OPCODE, beta_class(status=3), "PeerB")) == 1


def test_each_link_decodes_with_the_class_its_route_was_claimed_with(
    manager, free_ports, link_structures
):
    (path_a, _), (path_b, _) = link_structures
    connection = manager.create("multi", _config(free_ports(2), path_a, path_b))

    route_a, class_a = connection._claim_route("PeerA", SHARED_OPCODE)
    route_b, class_b = connection._claim_route("PeerB", SHARED_OPCODE)
    assert route_a == (PEER_A_CODE, SHARED_OPCODE) and route_b == (PEER_B_CODE, SHARED_OPCODE)
    assert type(connection._decode(class_a, b"\x01")).__name__ == "AlphaTrackReply"
    assert type(connection._decode(class_b, b"\x01")).__name__ == "BetaStatusReply"


def test_a_class_selector_must_be_the_links_own_layout(manager, free_ports, link_structures):
    (path_a, _), (path_b, _) = link_structures
    connection = manager.create("multi", _config(free_ports(2), path_a, path_b))
    alpha_reply = connection._claim_route("PeerA", SHARED_OPCODE)[1]

    assert connection._claim_route("PeerA", alpha_reply)[1] is alpha_reply
    with pytest.raises(ValueError, match="not by 'PeerB'"):
        connection._claim_route("PeerB", alpha_reply)


def test_an_opcode_the_link_does_not_define_is_refused_at_subscribe(
    manager, free_ports, link_structures
):
    (path_a, _), (path_b, _) = link_structures
    connection = manager.create("multi", _config(free_ports(2), path_a, path_b))
    with pytest.raises(IRSNotFoundError, match="known opcodes"):
        connection.receive_message(SHARED_OPCODE + 1, unit_name="PeerA", timeout=0.1)


def test_every_per_unit_structures_file_is_loaded(manager, free_ports, link_structures):
    (path_a, ns_a), (path_b, ns_b) = link_structures
    manager.create("multi", _config(free_ports(2), path_a, path_b))
    assert ns_a in sys.modules and ns_b in sys.modules


def test_two_files_on_one_link_that_disagree_about_a_route_are_refused(
    manager, free_ports, link_structures
):
    """Merging is never shadowing: one link cannot mean two layouts by one route."""
    (path_a, _), (path_b, _) = link_structures
    with pytest.raises(IRSAmbiguousError, match=f"opCode={SHARED_OPCODE}"):
        manager.create("multi", _config(free_ports(2), [path_a, path_b], path_b))


def test_connection_level_structures_with_two_units_is_rejected(free_ports, link_structures):
    """A structures file describes ONE link, so it cannot be a connection-wide
    default once there is more than one link to apply it to."""
    (path_a, _), _ = link_structures
    with pytest.raises(ValueError, match="exactly one unit"):
        ConnectionConfig.from_json(_config(free_ports(2), connection_level=path_a))


# --------------------------------------------------------------------------- #
# The config's unit names are the structures file's unit names
# --------------------------------------------------------------------------- #
def test_a_unit_code_that_disagrees_with_the_file_is_refused(manager, free_ports, link_structures):
    (path_a, _), (path_b, _) = link_structures
    with pytest.raises(ValueError, match=rf"\['PeerA'\]\['unitCode'\] = 99, but .* defines PeerA = {PEER_A_CODE}"):
        manager.create("multi", _config(free_ports(2), path_a, path_b, code_a=99))
    with pytest.raises(KeyError):
        manager.get("multi")


def test_a_unit_name_the_file_does_not_define_is_refused(manager, free_ports, link_structures):
    (path_a, _), (path_b, _) = link_structures
    with pytest.raises(ValueError, match="defines no unit named 'Alpha'"):
        manager.create("multi", _config(free_ports(2), path_a, path_b, name_a="Alpha"))


def test_our_own_code_must_be_a_unit_of_every_link(manager, free_ports, link_structures):
    (path_a, _), (path_b, _) = link_structures
    with pytest.raises(ValueError, match=r"config\['unitCode'\] = 77 is not a unit of"):
        manager.create("multi", _config(free_ports(2), path_a, path_b, own_code=77))


# --------------------------------------------------------------------------- #
# A pair unit speaks the IRS of the unit it pairs (tiful_to_dtu: MFD = pair(DTU, 0x03))
# --------------------------------------------------------------------------- #
TIFUL_TO_DTU = str(Path(core.IRS.__file__).parent / "Structures" / "Tiful" / "tiful_to_dtu.py")


def _pair_config(port, own_code, peer_name, peer_code):
    return {
        "protocol": "udp", "side": "server", "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "unitCode": own_code,
        "connections": {peer_name: {"port": port, "unitCode": peer_code, "Structures": [TIFUL_TO_DTU]}},
    }


def test_a_message_from_a_pair_is_parsed_as_the_paired_units_message(manager, free_ports):
    """(0x03, 0x0012) is MFD sending us DTU's SetGeneralFlag."""
    connection = manager.create("tiful", _pair_config(free_ports(1)[0], 0x01, "MFD", 0x03))
    route, message_class = connection._claim_route("MFD", 0x0012)
    assert route == (0x03, 0x0012) and message_class.__name__ == "SetGeneralFlag"
    assert connection._claim_route("MFD", message_class)[1] is message_class
    assert connection._decode(message_class, b"\x02\x07").value == 7


def test_a_pair_sends_the_paired_units_messages(manager, free_ports):
    connection = manager.create("mfd", _pair_config(free_ports(1)[0], 0x03, "Tiful", 0x01))
    set_flag = connection.interface_for("Tiful").message_class(0x02, 0x0012)
    general_flag = connection.interface_for("Tiful").message_class(0x01, 0x0012)
    assert connection._send_key(set_flag(), None) == 0x0012
    with pytest.raises(ValueError, match="sent by unit 1"):
        connection._send_key(general_flag(), None)
