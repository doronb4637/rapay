"""
The namespaced registry: which structures module answers a route.

A structures file describes ONE link, so the same (unitCode, opCode) may
legitimately mean different things in two different files. These tests pin the
rules that make that safe: a scoped lookup resolves inside its own module, an
unscoped one raises rather than silently picking a winner, and the pair alias
stays confined to the module that declared it.

`IRS.REGISTRY` is process-global and the rest of the suite depends on its
contents, so every test here registers into throwaway namespaces and the
autouse fixture restores both dicts afterwards.
"""
from __future__ import annotations

import pytest

from core.IRS import Message, UInt16
from core.IRS.REGISTRY import (
    PAIR_REGISTRY,
    STRUCTURE_REGISTRY,
    get_specification,
    namespaces_for,
    register_message,
    register_pair,
)
from core.IRS.irs_parser import (
    IRSAmbiguousError,
    IRSNotFoundError,
    get_message_class,
    is_irs_exist,
    known_unit_codes,
    list_routes,
)

UNIT = 220          # this file's own range, clear of every other suite/module
OTHER_UNIT = 221
ALIAS_UNIT = 222
OPCODE = 900

NS_A = "tests._ns_alpha"
NS_B = "tests._ns_beta"


class Alpha(Message):
    value: int = UInt16


class Beta(Message):
    other: int = UInt16


@pytest.fixture(autouse=True)
def isolated_registry():
    """Snapshot/restore, so these registrations never leak into another test."""
    structures = {name: {code: dict(ops) for code, ops in units.items()}
                  for name, units in STRUCTURE_REGISTRY.items()}
    pairs = {name: dict(aliases) for name, aliases in PAIR_REGISTRY.items()}
    try:
        yield
    finally:
        STRUCTURE_REGISTRY.clear()
        STRUCTURE_REGISTRY.update(structures)
        PAIR_REGISTRY.clear()
        PAIR_REGISTRY.update(pairs)


def _register_both() -> None:
    """The situation this whole change exists for: two files, one route."""
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    register_message(UNIT, OPCODE, Beta, namespace=NS_B)


# --------------------------------------------------------------------------- #
# Namespace capture
# --------------------------------------------------------------------------- #
def test_namespace_defaults_to_the_calling_module():
    """A structures file registers itself without naming itself -- which is why
    no existing structures file needed editing."""
    register_message(UNIT, OPCODE, Alpha)
    assert __name__ in STRUCTURE_REGISTRY
    assert get_specification(__name__)[UNIT][OPCODE] is Alpha


# --------------------------------------------------------------------------- #
# Scoped resolution vs. ambiguity
# --------------------------------------------------------------------------- #
def test_each_namespace_resolves_to_its_own_layout():
    _register_both()
    assert get_message_class(UNIT, OPCODE, NS_A) is Alpha
    assert get_message_class(UNIT, OPCODE, NS_B) is Beta
    # A scope may be a bare string or a list -- `Structures` is already a list.
    assert get_message_class(UNIT, OPCODE, [NS_B]) is Beta


def test_unscoped_lookup_of_a_duplicated_route_raises_naming_both():
    """The original bug: this used to return whichever module imported last."""
    _register_both()
    with pytest.raises(IRSAmbiguousError) as excinfo:
        get_message_class(UNIT, OPCODE)
    message = str(excinfo.value)
    assert NS_A in message and NS_B in message
    assert "Specification" in message      # tells the reader how to fix it


def test_single_namespace_unscoped_still_resolves():
    """Regression guard: adding namespaces must not make the ordinary
    one-module case require a scope."""
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    assert get_message_class(UNIT, OPCODE) is Alpha


def test_same_class_via_two_namespaces_is_not_ambiguous():
    """Two files importing one shared layout module agree; only genuinely
    different classes are a conflict."""
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    register_message(UNIT, OPCODE, Alpha, namespace=NS_B)
    assert get_message_class(UNIT, OPCODE) is Alpha


def test_is_irs_exist_propagates_ambiguity_instead_of_answering_false():
    """Reporting an ambiguous route as absent would put the silent wrong-layout
    bug straight back."""
    _register_both()
    with pytest.raises(IRSAmbiguousError):
        is_irs_exist(UNIT, OPCODE)
    assert is_irs_exist(UNIT, OPCODE, NS_A) is True
    assert is_irs_exist(UNIT, 0x7FFF, NS_A) is False


# --------------------------------------------------------------------------- #
# The two-tier miss contract, preserved from before namespaces
# --------------------------------------------------------------------------- #
def test_known_unit_with_unknown_opcode_returns_none():
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    assert get_message_class(UNIT, 0x7FFF, NS_A) is None


def test_unknown_unit_raises():
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    with pytest.raises(IRSNotFoundError):
        get_message_class(OTHER_UNIT, OPCODE, NS_A)


def test_scope_naming_an_unregistered_module_raises():
    """A typo in a config's `Structures` fails at the subscribing call, naming
    what is actually registered -- not silently at first message."""
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    with pytest.raises(IRSNotFoundError, match="registered no messages"):
        get_message_class(UNIT, OPCODE, "tests._does_not_exist")


# --------------------------------------------------------------------------- #
# Pair aliases -- one file serving several peer codes
# --------------------------------------------------------------------------- #
def test_pair_alias_resolves_through_its_own_namespace():
    """A file written for the UNIT link also serves ALIAS_UNIT."""
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    register_pair(UNIT, ALIAS_UNIT, namespace=NS_A)
    assert get_message_class(ALIAS_UNIT, OPCODE, NS_A) is Alpha
    assert get_message_class(ALIAS_UNIT, OPCODE) is Alpha


def test_pair_alias_does_not_leak_into_another_namespace():
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    register_pair(UNIT, ALIAS_UNIT, namespace=NS_A)
    register_message(OTHER_UNIT, OPCODE, Beta, namespace=NS_B)
    with pytest.raises(IRSNotFoundError):
        get_message_class(ALIAS_UNIT, OPCODE, NS_B)


def test_a_unit_with_its_own_layouts_is_not_redirected_by_an_alias():
    """The alias is WHOLE-UNIT: it only fires when the unit is absent, so
    `register_pair` never silently overrides a unit that speaks for itself."""
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    register_pair(UNIT, ALIAS_UNIT, namespace=NS_A)
    register_message(ALIAS_UNIT, OPCODE, Beta, namespace=NS_A)
    assert get_message_class(ALIAS_UNIT, OPCODE, NS_A) is Beta


def test_conflicting_aliases_in_two_namespaces_are_ambiguous_unscoped():
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    register_pair(UNIT, ALIAS_UNIT, namespace=NS_A)
    register_message(OTHER_UNIT, OPCODE, Beta, namespace=NS_B)
    register_pair(OTHER_UNIT, ALIAS_UNIT, namespace=NS_B)

    with pytest.raises(IRSAmbiguousError):
        get_message_class(ALIAS_UNIT, OPCODE)
    assert get_message_class(ALIAS_UNIT, OPCODE, NS_A) is Alpha
    assert get_message_class(ALIAS_UNIT, OPCODE, NS_B) is Beta


# --------------------------------------------------------------------------- #
# Introspection: listing must never raise, only resolution does
# --------------------------------------------------------------------------- #
def test_list_routes_shows_a_duplicated_route_twice_rather_than_raising():
    """A UI listing what a link can send has to show the conflict, not refuse
    to render."""
    _register_both()
    rows = [row for row in list_routes(UNIT) if row[0] == OPCODE]
    assert {row[2] for row in rows} == {NS_A, NS_B}
    assert {row[1] for row in rows} == {Alpha, Beta}


def test_list_routes_scoped_returns_only_that_module():
    _register_both()
    rows = list_routes(UNIT, NS_A)
    assert [(row[0], row[1], row[2]) for row in rows] == [(OPCODE, Alpha, NS_A)]


def test_namespaces_for_names_every_definer():
    _register_both()
    assert sorted(namespaces_for(UNIT, OPCODE)) == sorted([NS_A, NS_B])


def test_known_unit_codes_includes_aliases():
    register_message(UNIT, OPCODE, Alpha, namespace=NS_A)
    register_pair(UNIT, ALIAS_UNIT, namespace=NS_A)
    codes = known_unit_codes(NS_A)
    assert UNIT in codes and ALIAS_UNIT in codes
