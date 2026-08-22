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

from .annotations import *


""" GLOBAL MESSAGE REGISTRY """
#: namespace -> unitCode -> opCode -> message class
STRUCTURE_REGISTRY: dict[Namespace, dict[UnitCode, dict[OpCode, IrsMessage]]] = {}
#: A file written for the 1<->2 link can also describes 1<->143 for that we define 143 as a pair of 2.
#: namespace -> alias unitCode -> defined unitCode.
PAIR_REGISTRY: dict[Namespace, dict[UnitCode, UnitCode]] = {}


def _getting_struct_name(depth: int = 2) -> Namespace:
    """Return the calling <Structure>.py name.

    The depth level determines which frame in the call stack is inspected:
        * 0 = Here (inside this function)
        * 1 = Registration function
        * 2 = The calling structure module

    Args:
        depth (int, optional): The call stack depth to inspect. Defaults to 2.

    Returns:
        Namespace: The main namespace of the calling structure module.

    Raises:
        ImportError: If the structure name cannot be found in the frame's globals.
    """
    try:
        return sys._getframe(depth).f_globals["__name__"]
    except KeyError:
        raise ImportError(f"No Structure named found")


def register_message(unitCode: int, opCode: int, message: IrsMessage,
                     namespace: Namespace | None = None) -> None:
    """Register a message class under the calling structure module's namespace.

    This single function is meant to be imported and used by all message
    structure files. Re-registering the same (namespace, unitCode, opCode)
    overwrites silently to ensure module reloading remains idempotent.
    Duplication across namespaces is resolved at lookup rather than rejected
    here, as a configuration may legitimately never use both files together.

    Args:
        unitCode (int): The unit code associated with the message.
        opCode (int): The operation code associated with the message.
        message (IrsMessage): The message class to register.
        namespace (Namespace | None, optional): The namespace to register under.
            Defaults to the `__name__` of the calling module, allowing a structures
            file to register itself implicitly. Pass explicitly only when registering
            on another module's behalf (e.g., tests, generated code).
    """
    if namespace is None:
        namespace = _getting_struct_name()
    STRUCTURE_REGISTRY.setdefault(namespace, {}).setdefault(unitCode, {})[opCode] = message


def register_pair(unitCode: int, pair: UnitCode, namespace: Namespace | None = None) -> None:
    """Alias `pair` onto `unitCode`.

    Messages from the `pair` unit will be parsed using `unitCode`'s layouts,
    allowing a single structures file to serve several peer codes.

    Note:
        Pay attention to the argument order: the SECOND argument is the alias.
        For example, `register_pair(2, 14)` means "unit 14 speaks unit 2's IRS".

    Args:
        unitCode (int): The base unit code whose layouts will be used.
        pair (UnitCode): The alias unit code to map onto the base unit code.
        namespace (Namespace | None, optional): The namespace to register under.
            Defaults to the `__name__` of the calling module.
    """
    if namespace is None:
        namespace = _getting_struct_name()
    PAIR_REGISTRY.setdefault(namespace, {})[pair] = unitCode


""" Read helpers -- so callers never re-implement the merge rules themselves """
def get_registered_namespaces() -> list[Namespace]:
    """Return a list of all registered structure namespaces in import order."""
    return list(STRUCTURE_REGISTRY)


def get_messages(namespace: Namespace) -> dict[UnitCode, dict[OpCode, IrsMessage]]:
    """Return the message layouts registered under a specific namespace.

    Args:
        namespace (Namespace): The namespace to retrieve layouts for.

    Returns:
        dict[UnitCode, dict[OpCode, IrsMessage]]: A nested dictionary mapping unit
            codes to operation codes and their corresponding message parsers.
            Returns an empty dictionary if the namespace has not registered anything.
    """
    return STRUCTURE_REGISTRY.get(namespace, {})


def namespaces_for(unitCode: UnitCode, opCode: OpCode) -> list[Namespace]:
    """Return a list of structure modules that define the exact route for a given unit and operation code.
    
    This function returns direct hits only. The alias fallback mechanism belongs
    to `irs_parser`, which owns the lookup semantics.
    
    Note:
        This function is intended strictly for testing purposes (e.g., identifying
        ambiguity errors in tests) and will raise an error if called outside of
        a pytest environment.
    Raises:
        RuntimeError: If the function is called outside of a pytest run.
    """
    # Safeguard to ensure this is only ever used during testing
    if "pytest" not in sys.modules:
        raise RuntimeError("namespaces_for() is a test-only utility and cannot be used in production.")
    return [
        namespace
        for namespace, units in STRUCTURE_REGISTRY.items()
        if opCode in units.get(unitCode, {})
    ]
