"""
IRS -- `@Definition` and `Interface`: what one link's structures define, read
straight off the module, with no process-wide registry in between.
"""
import types

import pytest

from core.IRS import Byte, Definition, Message, is_definition, pair
from core.IRS.irs_parser import Interface, IRSAmbiguousError, IRSNotFoundError, parse


def _module(name: str, **members) -> types.ModuleType:
    module = types.ModuleType(name)
    vars(module).update(members)
    return module


@Definition(unitCode=1, opCode=0x10)
class Alpha(Message):
    value: int = Byte


@Definition(unitCode=2, opCode=0x10)
class AlphaReply(Message):
    ok: int = Byte


@Definition(unitCode=1, opCode=0x10)
class Rival(Message):
    other: int = Byte


class Undecorated(Message):
    value: int = Byte


# --------------------------------------------------------------------------- #
# @Definition
# --------------------------------------------------------------------------- #
def test_definition_binds_the_sender_and_the_opcode():
    assert (Alpha._unitCode, Alpha._opCode) == (1, 0x10)
    assert is_definition(Alpha) and not is_definition(Undecorated)


def test_an_undecorated_subclass_does_not_inherit_the_route():
    class Child(Alpha):
        pass
    assert not is_definition(Child)


# --------------------------------------------------------------------------- #
# Interface.from_module
# --------------------------------------------------------------------------- #
def test_from_module_reads_units_and_layouts():
    interface = Interface.from_module(_module(
        "link", Tiful=1, DTU=2, MFD=3, ENDIAN="<", _private=9,
        Alpha=Alpha, AlphaReply=AlphaReply, Undecorated=Undecorated))
    assert interface.namespaces == ("link",)
    assert dict(interface.units) == {"Tiful": 1, "DTU": 2, "MFD": 3}
    assert dict(interface.messages) == {(1, 0x10): Alpha, (2, 0x10): AlphaReply}


def test_two_names_for_one_class_are_one_layout():
    interface = Interface.from_module(_module("link", Alpha=Alpha, Alias=Alpha))
    assert dict(interface.messages) == {(1, 0x10): Alpha}


def test_two_classes_on_one_route_in_one_file_are_refused():
    with pytest.raises(IRSAmbiguousError, match="both Alpha and Rival"):
        Interface.from_module(_module("link", Alpha=Alpha, Rival=Rival))


# --------------------------------------------------------------------------- #
# Interface.merge -- one link listing several files
# --------------------------------------------------------------------------- #
def test_merge_unions_units_and_layouts():
    merged = Interface.merge(Interface.from_module(_module("a", Tiful=1, Alpha=Alpha)),
                             Interface.from_module(_module("b", DTU=2, AlphaReply=AlphaReply)))
    assert merged.namespaces == ("a", "b")
    assert dict(merged.units) == {"Tiful": 1, "DTU": 2}
    assert merged.message_class(2, 0x10) is AlphaReply


def test_merge_refuses_two_files_giving_one_route_two_layouts():
    with pytest.raises(IRSAmbiguousError, match="opCode=16"):
        Interface.merge(Interface.from_module(_module("a", Alpha=Alpha)),
                        Interface.from_module(_module("b", Rival=Rival)))


def test_merge_refuses_two_files_giving_one_unit_name_two_codes():
    with pytest.raises(IRSAmbiguousError, match="unit 'DTU' is 2 in a but 3 in b"):
        Interface.merge(Interface.from_module(_module("a", DTU=2, Alpha=Alpha)),
                        Interface.from_module(_module("b", DTU=3, AlphaReply=AlphaReply)))


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def test_message_class_names_what_the_unit_does_send():
    interface = Interface.from_module(_module("link", Alpha=Alpha))
    with pytest.raises(IRSNotFoundError, match=r"known opcodes: \[16\]"):
        interface.message_class(1, 0x11)


def test_routes_and_unit_codes():
    interface = Interface.from_module(_module("link", MFD=3, Alpha=Alpha, AlphaReply=AlphaReply))
    assert interface.routes(1) == [(0x10, Alpha)]
    assert interface.unit_codes() == [1, 2, 3]


def test_parse_uses_the_given_class():
    message = parse(AlphaReply, b"\x05")
    assert type(message) is AlphaReply and message.ok == 5


# --------------------------------------------------------------------------- #
# pair -- a unit that speaks another unit's IRS
# --------------------------------------------------------------------------- #
def test_a_pair_is_its_own_unit_code():
    mfd = pair(2, 0x03)
    assert mfd == 3 and mfd.impose == 2


def test_a_pair_resolves_to_the_layouts_of_the_unit_it_speaks():
    interface = Interface.from_module(_module("link", DTU=2, MFD=pair(2, 3), AlphaReply=AlphaReply))
    assert dict(interface.units) == {"DTU": 2, "MFD": 3}
    assert interface.speaks_as(3) == 2 and interface.speaks_as(2) == 2
    assert interface.message_class(3, 0x10) is AlphaReply
    assert interface.routes(3) == [(0x10, AlphaReply)]
    assert interface.unit_codes() == [2, 3]


def test_a_pair_names_itself_when_the_route_is_missing():
    interface = Interface.from_module(_module("link", DTU=2, MFD=pair(2, 3), AlphaReply=AlphaReply))
    with pytest.raises(IRSNotFoundError, match=r"Unit: 3 \(pair of 2\).*known opcodes: \[16\]"):
        interface.message_class(3, 0x11)

