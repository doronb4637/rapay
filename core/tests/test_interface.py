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


# --------------------------------------------------------------------------- #
# Interface.from_module
# --------------------------------------------------------------------------- #
def test_from_module_reads_layouts_and_ignores_other_constants():
    interface = Interface.from_module(_module(
        "link", Tiful=1, ENDIAN="<", _private=9,
        Alpha=Alpha, AlphaReply=AlphaReply, Undecorated=Undecorated))
    assert interface.namespaces == ("link",)
    assert dict(interface.messages) == {(1, 0x10): Alpha, (2, 0x10): AlphaReply}
    assert interface.unit_codes() == [1, 2]


def test_two_names_for_one_class_are_one_layout():
    interface = Interface.from_module(_module("link", Alpha=Alpha, Alias=Alpha))
    assert dict(interface.messages) == {(1, 0x10): Alpha}


def test_two_classes_on_one_route_in_one_file_are_refused():
    with pytest.raises(IRSAmbiguousError, match="both Alpha and Rival"):
        Interface.from_module(_module("link", Alpha=Alpha, Rival=Rival))


# --------------------------------------------------------------------------- #
# Interface.merge -- one link listing several files
# --------------------------------------------------------------------------- #
def test_merge_unions_layouts_and_pairs():
    merged = Interface.merge(Interface.from_module(_module("a", Alpha=Alpha)),
                             Interface.from_module(_module("b", PAIRS={3: 2}, AlphaReply=AlphaReply)))
    assert merged.namespaces == ("a", "b")
    assert merged.message_class(1, 0x10) is Alpha
    assert merged.message_class(3, 0x10) is AlphaReply


def test_merge_refuses_two_files_giving_one_route_two_layouts():
    with pytest.raises(IRSAmbiguousError, match="opCode=16"):
        Interface.merge(Interface.from_module(_module("a", Alpha=Alpha)),
                        Interface.from_module(_module("b", Rival=Rival)))


def test_merge_keeps_a_pair_inside_the_file_that_declares_it():
    """File a pairs 0x8F to 2; file b gives 0x8F layouts of its own. Merged on
    one link, b's 0x8F route must stay b's, not be redirected to unit 2."""
    @Definition(unitCode=0x8F, opCode=0x20)
    class Own(Message):
        value: int = Byte
    merged = Interface.merge(Interface.from_module(_module("a", PAIRS={0x8F: 2}, Alpha=Alpha, AlphaReply=AlphaReply)),
                             Interface.from_module(_module("b", Alpha=Alpha, Own=Own)))
    assert merged.message_class(0x8F, 0x20) is Own
    assert merged.message_class(0x8F, 0x10) is AlphaReply
    assert merged.speaks_as(0x8F, 0x20) == 0x8F and merged.speaks_as(0x8F, 0x10) == 2
    assert merged.routes(0x8F) == [(0x10, AlphaReply), (0x20, Own)]


def test_merge_refuses_a_pair_route_another_file_gives_a_different_layout():
    @Definition(unitCode=3, opCode=0x10)
    class Clash(Message):
        value: int = Byte
    with pytest.raises(IRSAmbiguousError, match="unitCode=3, opCode=16"):
        Interface.merge(Interface.from_module(_module("a", PAIRS={3: 2}, AlphaReply=AlphaReply)),
                        Interface.from_module(_module("b", Clash=Clash)))


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def test_message_class_names_what_the_unit_does_send():
    interface = Interface.from_module(_module("link", Alpha=Alpha))
    with pytest.raises(IRSNotFoundError, match=r"known opcodes: \[16\]"):
        interface.message_class(1, 0x11)


def test_routes_and_unit_codes():
    interface = Interface.from_module(_module("link", PAIRS={3: 2}, Alpha=Alpha, AlphaReply=AlphaReply))
    assert interface.routes(1) == [(0x10, Alpha)]
    assert interface.unit_codes() == [1, 2, 3]


def test_parse_uses_the_given_class():
    message = parse(AlphaReply, b"\x05")
    assert type(message) is AlphaReply and message.ok == 5


# --------------------------------------------------------------------------- #
# PAIRS -- a unit that speaks another unit's IRS
# --------------------------------------------------------------------------- #
def test_a_pair_resolves_to_the_layouts_of_the_unit_it_speaks():
    interface = Interface.from_module(_module("link", PAIRS={3: 2}, AlphaReply=AlphaReply))
    assert interface.speaks_as(3, 0x10) == 2 and interface.speaks_as(2, 0x10) == 2
    assert interface.message_class(3, 0x10) is AlphaReply
    assert interface.routes(3) == [(0x10, AlphaReply)]
    assert interface.unit_codes() == [2, 3]


def test_a_pair_names_itself_when_the_route_is_missing():
    interface = Interface.from_module(_module("link", PAIRS={3: 2}, AlphaReply=AlphaReply))
    with pytest.raises(IRSNotFoundError, match=r"Unit: 3 .*known opcodes: \[16\]"):
        interface.message_class(3, 0x11)


def test_a_pair_that_defines_its_own_layouts_is_refused():
    with pytest.raises(IRSAmbiguousError, match="unit 1 is paired to 2 but also defines its own message"):
        Interface.from_module(_module("link", PAIRS={1: 2}, Alpha=Alpha))


def test_a_pair_to_a_unit_the_file_does_not_send_is_refused():
    with pytest.raises(ValueError, match="unit 3 is paired to 2, which sends no message"):
        Interface.from_module(_module("link", PAIRS={3: 2}, Alpha=Alpha))


@pytest.mark.parametrize("pairs", [[(3, 2)], {"MFD": 2}, {3: "DTU"}, {3: True}])
def test_malformed_pairs_are_refused(pairs):
    with pytest.raises(TypeError, match="link.PAIRS must be a dict"):
        Interface.from_module(_module("link", PAIRS=pairs))
