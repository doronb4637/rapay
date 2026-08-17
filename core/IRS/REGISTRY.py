"""
The message registry: storage and registration only.

Every lookup rule -- scoping, the pair-alias fallback, ambiguity -- lives in
`IRS.irs_parser`, which is also the only public way to read this module. That
split is deliberate: this file must stay importable from `irs_parser` without a
cycle, and having exactly one place that decides *which* layout answers a route
is what makes the answer predictable.

Layouts are keyed by NAMESPACE first, because a structures file describes one
link -- one specific server to one specific client (multicast being the
exception, where one sender fans out to many receivers over a single shared
IRS). A process that talks to two peers loads two structures files, and both
register layouts under OUR unit code; keying by unit code alone meant the second
import silently erased the first wherever the two shared an opcode.

The namespace is captured automatically from the module that calls
`register_message`, so a structures file needs no changes at all: importing it
IS the namespaced registration.
"""
import sys

from core.annotations import *


""" GLOBAL MESSAGE REGISTRY """
#: namespace -> unitCode -> opCode -> message class
STRUCTURE_REGISTRY: dict[Namespace, dict[UnitCode, dict[OpCode, IrsMessage]]] = {}
#: A file written for the 1<->2 link can also describes 1<->143 for that we define 143 as a pair of 2.
#: namespace -> alias unitCode -> defined unitCode.
PAIR_REGISTRY: dict[Namespace, dict[UnitCode, UnitCode]] = {}


def _getting_struct_name(depth: int = 2) -> Namespace:
    """`__name__` of the frame `depth` levels up (0 = here, 1 = the register_*
    function, 2 = the structures module that called it)."""
    try:
        return sys._getframe(depth).f_globals.get("__name__") or "<unknown>"
    except ValueError:
        return "<unknown>"


def register_message(unitCode: int, opCode: int, message: IrsMessage,
                     namespace: Namespace | None = None) -> None:
    """
    Registers a message parser under the calling structures module's namespace.
    This single function will be imported and used by all message structure files.

    `namespace` defaults to the `__name__` of the module that called this, so a
    structures file registers itself correctly without naming itself. Pass it
    explicitly only when registering on another module's behalf (tests,
    generated code).

    Re-registering the same (namespace, unitCode, opCode) overwrites silently --
    that is a module reload, and has to stay idempotent. Duplication ACROSS
    namespaces is the interesting case, and it is resolved at lookup rather than
    rejected here: a config may legitimately never use both files together.
    """
    if namespace is None:
        namespace = _getting_struct_name()
    STRUCTURE_REGISTRY.setdefault(namespace, {}).setdefault(unitCode, {})[opCode] = message


def register_pair(unitCode: int, pair: UnitCode, namespace: Namespace | None = None) -> None:
    """
    Alias `pair` onto `unitCode`: messages from unit `pair` are parsed with unit
    `unitCode`'s layouts, so one structures file can serve several peer codes.

    Note the argument order -- the SECOND argument is the alias.
    `register_pair(2, 14)` means "unit 14 speaks unit 2's IRS".
    """
    if namespace is None:
        namespace = _getting_struct_name()
    PAIR_REGISTRY.setdefault(namespace, {})[pair] = unitCode


""" Read helpers -- so callers never re-implement the merge rules themselves """
def registered_namespaces() -> list[Namespace]:
    """Every structures module that has registered anything, in import order."""
    return list(STRUCTURE_REGISTRY)


def messages_in(namespace: Namespace) -> dict[UnitCode, dict[OpCode, IrsMessage]]:
    """One namespace's layouts. Empty dict when it registered nothing."""
    return STRUCTURE_REGISTRY.get(namespace, {})


def namespaces_for(unitCode: UnitCode, opCode: OpCode) -> list[Namespace]:
    """Which structures modules define this exact route -- what an ambiguity
    error needs to name. Direct hits only; the alias fallback belongs to
    `irs_parser`, which owns lookup semantics."""
    return [
        namespace
        for namespace, units in STRUCTURE_REGISTRY.items()
        if opCode in units.get(unitCode, {})
    ]
