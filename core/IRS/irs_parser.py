import logging

from .buffers import BinaryReader
from .REGISTRY import *
from .annotations import *

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
    """Normalize the provided namespace scope into a tuple."""
    if namespace is None:
        return ()
    if isinstance(namespace, str):
        return (namespace,)
    return tuple(namespace)


def _unit_messages(namespace: Namespace, unitCode: UnitCode) -> dict[OpCode, IrsMessage] | None:
    """Return the message layouts for a unit code within a namespace, resolving pair aliases.
    Note:
        The alias resolution is strictly whole-unit

    Args:
        namespace (Namespace): The namespace catalog to search within.
        unitCode (UnitCode): The specific unit code whose layouts are requested.

    Returns:
        dict[OpCode, IrsMessage] | None: A dictionary mapping operation codes to
            their corresponding message layouts. Returns None if the unit code is
            not found and has no registered alias in this namespace.
    """
    specification = get_specification(namespace)
    messages = specification.get(unitCode)
    if messages is not None:
        return messages
    paired_unitCode = PAIR_REGISTRY.get(namespace, {}).get(unitCode)
    return specification.get(paired_unitCode)


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
        namespace: the Specification module(s)-(DLL files) this link uses. Omit to search every
            registered module.

    Returns:
        IrsMessage: The uninitialized message class,
        or None when the unit message structure exists but the message
        is not found.
    Raises:
        IRSNotFoundError: when the unit message specification doesn't exists, or
            when `namespace` names a module that registered nothing.
        IRSAmbiguousError: when two specification modules define this route and
            none was named to choose between them.
    """
    if not namespace:
        scope = get_registered_namespaces()
    else:
        scope = _normalise_scope(namespace)
        unknown = [name for name in scope if name not in STRUCTURE_REGISTRY]
        if unknown:
            raise IRSNotFoundError(
                f"Structures module(s) {unknown} registered no messages! "
                f"known modules: {sorted(STRUCTURE_REGISTRY)}")

    matched: dict[Namespace, IrsMessage] = {}
    unit_known = False
    for name in scope:
        messages = _unit_messages(name, unitCode)
        if messages is None:
            continue
        unit_known = True
        message_class = messages.get(opCode)
        if message_class is not None:
            matched[name] = message_class
    if not unit_known:
        raise IRSNotFoundError(
            f"Unit: {unitCode}, Was not found! in unit messages{_scope_suffix(scope)}")
    # The same message is defined via two namespaces.
    if len(set(matched.values())) > 1:
        raise IRSAmbiguousError(
            f"(unitCode={unitCode}, opCode={opCode}) is defined by {len(matched)} specification files. "
            f"modules: {', '.join(sorted(matched))}.\n[*] A specification file describes ONE link, "
            f"so say which one this link uses: add \"Specification\" inside that unit's "
            f"connections[<name>] entry.")
    return next(iter(matched.values()), None)


""" API functions for IRS"""
def get_message_class(unitCode: UnitCode, opCode: OpCode,
                      namespace: NamespaceScope | None = None) -> IrsMessage | None:
    """The public form of `_get_message_class`"""
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
    Raises:
        IRSAmbiguousError: when two specification files define this (UnitCode, OpCode)
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


""" Defined for Gsim will be changed in time."""
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
