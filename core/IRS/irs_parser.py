import logging

from .buffers import BinaryReader
from .REGISTRY import *
from core.annotations import *

logger = logging.getLogger("parser")

""" Exception's """
class IRSNotFoundError(Exception):
    """Raised when the IRS instance or record is missing."""
    pass


class IRSDataError(Exception):
    """Raised when a header/payload cannot be built or parsed correctly."""


class IRSAmbiguousError(Exception):
    """Raised when a lookup matches a route defined by more than one structures
    module and was given no scope to choose between them.

    Deliberately NOT a subclass of IRSNotFoundError
    """


""" Private Helpers"""
def _normalise_scope(namespace: NamespaceScope | None) -> tuple[Namespace, ...]:
    """Accept one namespace or several; empty means "search every namespace".

    A unit's `Structures` is already a list, and a unit that declares none is
    byte-oriented -- it keeps the old process-wide behaviour rather than
    resolving to nothing.
    """
    if namespace is None:
        return ()
    if isinstance(namespace, str):
        return (namespace,)
    return tuple(namespace)


def _unit_messages(namespace: Namespace, unitCode: UnitCode) -> dict[OpCode, IrsMessage] | None:
    """One namespace's layouts for `unitCode`, following its pair alias.

    The alias is WHOLE-UNIT, not per-opcode: a unit that has its own entry never
    falls through. That is what makes `register_pair(2, 14)` mean "14 speaks 2's
    IRS" rather than "14 borrows whatever 2 has that 14 lacks".
    """
    units = STRUCTURE_REGISTRY.get(namespace, {})
    messages = units.get(unitCode)
    if messages is None:
        paired_unitCode = PAIR_REGISTRY.get(namespace, {}).get(unitCode)
        if paired_unitCode is not None:
            messages = units.get(paired_unitCode)
    return messages


def _scope_or_all(scope: tuple[Namespace, ...]) -> tuple[Namespace, ...]:
    if scope:
        return scope
    return tuple(STRUCTURE_REGISTRY)


def _scope_suffix(scope: tuple[Namespace, ...]) -> str:
    return f" [structures: {', '.join(scope)}]" if scope else ""


def _get_message_class(unitCode: UnitCode, opCode: OpCode,
                       namespace: NamespaceScope | None = None) -> IrsMessage | None:
    """Finds the message class.
    Args:
        unitCode (int):Code representing Sending-Unit/Our-Unit.
        opCode (int): The message code.
        namespace: the structures module(s) this link uses. Omit to search every
            registered module -- which is only unambiguous while no two of them
            define the same route.

    Returns:
        IrsMessage: The uninitialized message class,
        or None when the unit message structure exists but the message
        is not found.
    Raises:
        IRSNotFoundError: when the unit message structure doesn't exists, or
            when `namespace` names a module that registered nothing.
        IRSAmbiguousError: when two structures modules define this route and
            none was named to choose between them.
    """
    scope = _normalise_scope(namespace)
    unknown = [name for name in scope if name not in STRUCTURE_REGISTRY]
    if unknown:
        raise IRSNotFoundError(
            f"Structures module(s) {unknown} registered no messages! "
            f"known modules: {sorted(STRUCTURE_REGISTRY)}")

    matched: dict[Namespace, IrsMessage] = {}
    unit_known = False
    for name in _scope_or_all(scope):
        messages = _unit_messages(name, unitCode)
        if messages is None:
            continue
        unit_known = True
        message_class = messages.get(opCode)
        if message_class is not None:
            matched[name] = message_class

    # The same class reached through two namespaces is one answer, not a
    # conflict -- that is a shared module imported by both files.
    if len(set(matched.values())) > 1:
        raise IRSAmbiguousError(
            f"(unitCode={unitCode}, opCode={opCode}) is defined by {len(matched)} structures "
            f"modules: {', '.join(sorted(matched))}.\n[*] A structures file describes ONE link, "
            f"so say which one this link uses: add \"Structures\" inside that unit's "
            f"connections[<name>] entry.")
    if matched:
        return next(iter(matched.values()))

    # Nothing on this route. Keep the two-tier answer callers already rely on:
    # is the UNIT itself known (-> None, "opcode not implemented") or not?
    if not unit_known:
        raise IRSNotFoundError(
            f"Unit: {unitCode}, Was not found! in unit messages{_scope_suffix(scope)}")
    return None


""" API functions for IRS"""
def get_message_class(unitCode: UnitCode, opCode: OpCode,
                      namespace: NamespaceScope | None = None) -> IrsMessage | None:
    """The public form of `_get_message_class`, for callers that want the class
    itself rather than bytes -- schema introspection, tooling, UIs.

    Exists so nothing outside this module has to re-derive the scope/alias/
    ambiguity rules: a caller that resolved routes by hand would eventually
    disagree with what `parse_irs` actually does, which is the whole failure
    mode this registry is designed around.
    """
    return _get_message_class(unitCode, opCode, namespace)


def validate_irs(unitCode: UnitCode, opCode: OpCode,
                 namespace: NamespaceScope | None = None) -> None:
    """Validates the existence of the IRS.
    Raises:
        IRSNotFoundError: If the IRS does not exist.
        IRSAmbiguousError: If two structures modules define it and none was named."""
    check_unit_message_structure = _get_message_class(unitCode, opCode, namespace)
    if check_unit_message_structure is not None:
        return None
    raise IRSNotFoundError(
        f"Message of OpCode: {opCode}, Was not Implemented! for Unit: {unitCode}"
        f"{_scope_suffix(_normalise_scope(namespace))}")


def is_irs_exist(unitCode: UnitCode, opCode: OpCode,
                 namespace: NamespaceScope | None = None) -> bool:
    """ Returns whether the message is registered or not.

    An ambiguous route deliberately propagates rather than answering False: it
    exists twice, and pretending it does not is how the wrong layout gets used.
    """
    try:
        return _get_message_class(unitCode, opCode, namespace) is not None
    except IRSNotFoundError:
        return False


def irs_to_bytes(unitCode: UnitCode, opCode: OpCode, message: IrsMessage | dict,
                 namespace: NamespaceScope | None = None) -> bytes:
    if isinstance(message, dict):
        message_class = _get_message_class(unitCode, opCode, namespace)
        if message_class is not None:
            message = message_class.from_dict(message)
        else:
            raise IRSNotFoundError(
                f"Message of OpCode: {opCode}, Was not Implemented! for Unit: {unitCode}"
                f"{_scope_suffix(_normalise_scope(namespace))}")
    return message.to_bytes()


def parse_irs(unitCode: UnitCode, opCode: OpCode, payload: bytes,
              namespace: NamespaceScope | None = None) -> tuple[str, IrsMessage] | None:
    message_class = _get_message_class(unitCode, opCode, namespace)
    if message_class is not None:
        return message_class.__name__, message_class.from_bytes(BinaryReader(payload))
    raise IRSNotFoundError(
        f"Message of OpCode: {opCode}, Was not Implemented! for Unit: {unitCode}"
        f"{_scope_suffix(_normalise_scope(namespace))}")


""" Introspection -- listing must never raise on ambiguity, only resolution does """
def list_routes(unitCode: UnitCode,
                namespace: NamespaceScope | None = None) -> list[tuple[OpCode, IrsMessage, Namespace]]:
    """Every (opCode, message class, namespace) `unitCode` has a layout for.

    Two modules defining one opcode differently yield two entries here rather
    than an error: a UI listing what a link can send has to show the conflict,
    not refuse to render.
    """
    routes: list[tuple[OpCode, IrsMessage, Namespace]] = []
    for name in _scope_or_all(_normalise_scope(namespace)):
        messages = _unit_messages(name, unitCode)
        if not messages:
            continue
        routes.extend((opCode, message_class, name) for opCode, message_class in messages.items())
    return sorted(routes, key=lambda route: (route[0], route[2]))


def known_unit_codes(namespace: NamespaceScope | None = None) -> list[UnitCode]:
    """Every unit code with at least one registered layout, aliases included."""
    codes: set[UnitCode] = set()
    for name in _scope_or_all(_normalise_scope(namespace)):
        codes.update(STRUCTURE_REGISTRY.get(name, {}))
        codes.update(PAIR_REGISTRY.get(name, {}))
    return sorted(codes)
