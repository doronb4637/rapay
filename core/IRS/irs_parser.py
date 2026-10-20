"""
A link's IRS interface: the units and message layouts ONE structures file (or
the few a link lists together) defines.

There is no process-wide registry. A structures file tags each message with
`@Definition(unitCode=0x02, opCode=...)` -- the unit code is the number itself;
unit names live only in the connection config. A file may also declare
`PAIRS = {0x03: 0x02}`: unit 0x03 has no layouts of its own and speaks 0x02's
IRS, so (0x03, op) resolves to 0x02's message on `op`. `Interface.from_module`
reads both off the imported module and turns each pair into plain routes, so a
pair applies only to the file that declares it.
Whoever owns a link -- a `Connection` -- holds its own
`Interface`, so two links whose files reuse one (unitCode, opCode) never see
each other's layouts.
"""

import logging
from dataclasses import dataclass, field
from types import ModuleType
from typing import Mapping

from .buffers import BinaryReader
from .core import Message, is_definition
from .annotations import *

logger = logging.getLogger("parser")

Route = tuple[UnitCode, int]

#: Module-level `{pair code: code it speaks as}` a structures file may declare.
PAIRS_ATTRIBUTE = "PAIRS"


""" Exception's """
class IRSNotFoundError(Exception):
    """Raised when the IRS instance or record is missing."""
    pass


class IRSDataError(Exception):
    """Raised when a header/payload cannot be built or parsed correctly."""


class IRSAmbiguousError(Exception):
    """Raised when the structures files one link combines give one route two
    different layouts.

    Deliberately NOT a subclass of IRSNotFoundError
    """


def short_name(namespace: Namespace) -> str:
    """`<parent folder>.<file>` of a namespace, for error messages.

    The full namespace of a path-picked file embeds its whole directory chain.
    """
    return ".".join(namespace.split(".")[-2:])


@dataclass(frozen=True)
class Interface:
    """What one link can say: its layouts by route, pair routes already resolved."""
    namespaces: tuple[Namespace, ...]
    messages: Mapping[Route, IrsMessage] = field(default_factory=dict)

    @classmethod
    def from_module(cls, module: ModuleType) -> 'Interface':
        """Every `@Definition`'d message reachable from the module's globals is
        a layout (imported ones included, so a file may share messages with a
        sibling). The module's optional `PAIRS` dict gives each pair code a
        route for every layout THIS file gives the unit it speaks as, so a pair
        never leaks into another file merged onto the same link.

        The file is trusted as written; `core.tools.validate_interface` checks
        it offline.
        """
        defined: dict[Route, IrsMessage] = {
            (value._unitCode, value._opCode): value
            for name, value in vars(module).items() if not name.startswith("_") and is_definition(value)}
        messages = dict(defined)
        for pairCode, speaksCode in getattr(module, PAIRS_ATTRIBUTE, {}).items():
            for (unit, op), message_class in defined.items():
                if unit == speaksCode:
                    messages[(pairCode, op)] = message_class
        return cls((module.__name__,), messages)

    @classmethod
    def merge(cls, *interfaces: 'Interface') -> 'Interface':
        """One link built from several structures files. A route two of them
        define differently is refused, not shadowed. Each file's pairs were
        resolved in `from_module`, so they only cover that file's layouts."""
        namespaces: list[Namespace] = []
        messages: dict[Route, IrsMessage] = {}
        for interface in interfaces:
            source = ", ".join(map(short_name, interface.namespaces))
            namespaces.extend(interface.namespaces)
            for route, message_class in interface.messages.items():
                _add_message(messages, route, message_class, source)
        return cls(tuple(namespaces), messages)

    def speaks_as(self, unitCode: UnitCode, opCode: int) -> UnitCode:
        """The unit whose layout `unitCode` sends on `opCode`: the paired unit's
        for a pair route, else (and for an unknown route) `unitCode` itself."""
        message_class = self.messages.get((unitCode, opCode))
        return unitCode if message_class is None else message_class._unitCode

    def message_class(self, unitCode: UnitCode, opCode: int) -> IrsMessage:
        """The layout `unitCode` sends under `opCode`.

        Raises:
            IRSNotFoundError: this link defines no such route.
        """
        message_class = self.messages.get((unitCode, opCode))
        if message_class is not None:
            return message_class
        known = sorted(op for unit, op in self.messages if unit == unitCode)
        raise IRSNotFoundError(
            f"Message of OpCode: {opCode}, Was not Implemented! for Unit: {unitCode} "
            f"[structures: {', '.join(map(short_name, self.namespaces))}]; known opcodes: {known}")

    def routes(self, unitCode: UnitCode) -> list[tuple[int, IrsMessage]]:
        """Every (opCode, message class) `unitCode` sends on this link, by opcode."""
        return sorted(((op, message_class) for (unit, op), message_class in self.messages.items()
                       if unit == unitCode), key=lambda route: route[0])

    def unit_codes(self) -> list[UnitCode]:
        """Every unit code that sends a layout on this link, directly or as a pair."""
        return sorted({unit for unit, _op in self.messages})


def _add_message(messages: dict[Route, IrsMessage], route: Route, message_class: IrsMessage, source: str) -> None:
    existing = messages.get(route)
    if existing is not None and existing is not message_class:
        raise IRSAmbiguousError(
            f"(unitCode={route[0]}, opCode={route[1]}) is both {existing.__name__} and "
            f"{message_class.__name__} [structures: {source}]")
    messages[route] = message_class


def parse(message_class: IrsMessage, payload: bytes) -> Message:
    """Wire payload -> an instance of the layout the route was subscribed with."""
    return message_class.from_bytes(BinaryReader(payload))
