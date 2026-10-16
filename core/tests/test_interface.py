"""
IRS -- `@Definition` and `Interface`: what one link's structures define, read
straight off the module, with no process-wide registry in between.
"""
import types

import pytest

from core.IRS import Byte, Definition, Message, is_definition
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


@pytest.mark.parametrize("unit, op", [("1", 1), (1, None), (True, 1)])
def test_definition_refuses_a_non_int_code(unit, op):
    with pytest.raises(TypeError, match="must be an int"):
        Definition(unitCode=unit, opCode=op)


def test_definition_only_decorates_messages():
    with pytest.raises(TypeError, match="Message subclasses"):
        Definition(unitCode=1, opCode=1)(object)


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
