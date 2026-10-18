"""
A link's IRS interface: the units and message layouts ONE structures file (or
the few a link lists together) defines.

There is no process-wide registry. A structures file declares its units as
module-level int constants (`DTU = 0x02`) and tags each message with
`@Definition(unitCode=..., opCode=...)`; `Interface.from_module` reads both off
the imported module. A unit declared `MFD = pair(DTU, 0x03)` has no layouts of
its own: it speaks DTU's IRS, so (0x03, op) resolves to DTU's message on `op`.
Whoever owns a link -- a `Connection` -- holds its own
`Interface`, so two links whose files reuse one (unitCode, opCode) never see
each other's layouts.
"""

import logging
from dataclasses import dataclass, field
from types import ModuleType
from typing import Mapping

from .buffers import BinaryReader
from .core import Message, Pair, is_definition
from .annotations import *

logger = logging.getLogger("parser")

Route = tuple[UnitCode, int]


""" Exception's """
class IRSNotFoundError(Exception):
    """Raised when the IRS instance or record is missing."""
    pass


class IRSDataError(Exception):
    """Raised when a header/payload cannot be built or parsed correctly."""


class IRSAmbiguousError(Exception):
    """Raised when the structures files one link combines give one route, or
    one unit name, two different meanings.

    Deliberately NOT a subclass of IRSNotFoundError
    """


def short_name(namespace: Namespace) -> str:
    """`<parent folder>.<file>` of a namespace, for error messages.

    The full namespace of a path-picked file embeds its whole directory chain.
    """
    return ".".join(namespace.split(".")[-2:])


@dataclass(frozen=True)
class Interface:
    """What one link can say: its units by name and its layouts by route."""
    namespaces: tuple[Namespace, ...]
    units: Mapping[str, UnitCode] = field(default_factory=dict)
    messages: Mapping[Route, IrsMessage] = field(default_factory=dict)
    pairs: Mapping[UnitCode, UnitCode] = field(default_factory=dict)

    @classmethod
    def from_module(cls, module: ModuleType) -> 'Interface':
        """Every public module-level int is a unit; every `@Definition`'d
        message reachable from the module's globals is a layout (imported ones
        included, so a file may share messages with a sibling)."""
        units: dict[str, UnitCode] = {}
        messages: dict[Route, IrsMessage] = {}
        pairs: dict[UnitCode, UnitCode] = {}
        for name, value in vars(module).items():
            if name.startswith("_"):
                continue
            if isinstance(value, int) and not isinstance(value, bool):
                units[name] = int(value)
                if isinstance(value, Pair):
                    pairs[int(value)] = value.impose
            elif is_definition(value):
                _add_message(messages, value, short_name(module.__name__))
        return cls((module.__name__,), units, messages, pairs)

    @classmethod
    def merge(cls, *interfaces: 'Interface') -> 'Interface':
        """One link built from several structures files. A route or unit name
        two of them define differently is refused, not shadowed."""
        namespaces: list[Namespace] = []
        units: dict[str, UnitCode] = {}
        unit_source: dict[str, str] = {}
        messages: dict[Route, IrsMessage] = {}
        pairs: dict[UnitCode, UnitCode] = {}
        for interface in interfaces:
            source = ", ".join(map(short_name, interface.namespaces))
            namespaces.extend(interface.namespaces)
            for name, unitCode in interface.units.items():
                if units.get(name, unitCode) != unitCode:
                    raise IRSAmbiguousError(f"unit {name!r} is {units[name]} in {unit_source[name]} but {unitCode} in {source}")
                units[name] = unitCode
                unit_source.setdefault(name, source)
            for message_class in interface.messages.values():
                _add_message(messages, message_class, source)
            for unitCode, imposeCode in interface.pairs.items():
                pairs[unitCode] = imposeCode
        return cls(tuple(namespaces), units, messages, pairs)

    def speaks_as(self, unitCode: UnitCode) -> UnitCode:
        """The unit whose IRS `unitCode` speaks: the paired unit, else itself."""
        return self.pairs.get(unitCode, unitCode)

    def message_class(self, unitCode: UnitCode, opCode: int) -> IrsMessage:
        """The layout `unitCode` sends under `opCode`.

        Raises:
            IRSNotFoundError: this link defines no such route.
        """
        speaker = self.speaks_as(unitCode)
        message_class = self.messages.get((speaker, opCode))
        if message_class is not None:
            return message_class
        known = sorted(op for unit, op in self.messages if unit == speaker)
        paired = f" (pair of {speaker})" if speaker != unitCode else ""
        raise IRSNotFoundError(
            f"Message of OpCode: {opCode}, Was not Implemented! for Unit: {unitCode}{paired} "
            f"[structures: {', '.join(map(short_name, self.namespaces))}]; known opcodes: {known}")

    def routes(self, unitCode: UnitCode) -> list[tuple[int, IrsMessage]]:
        """Every (opCode, message class) `unitCode` sends on this link, by opcode."""
        speaker = self.speaks_as(unitCode)
        return sorted(((op, message_class) for (unit, op), message_class in self.messages.items()
                       if unit == speaker), key=lambda route: route[0])

    def unit_codes(self) -> list[UnitCode]:
        """Every unit code this link names or sends from."""
        return sorted({*self.units.values(), *(unit for unit, _op in self.messages)})


def _add_message(messages: dict[Route, IrsMessage], message_class: IrsMessage, source: str) -> None:
    route = (message_class._unitCode, message_class._opCode)
    existing = messages.get(route)
    if existing is not None and existing is not message_class:
        raise IRSAmbiguousError(
            f"(unitCode={route[0]}, opCode={route[1]}) is both {existing.__name__} and "
            f"{message_class.__name__} [structures: {source}]")
    messages[route] = message_class


def parse(message_class: IrsMessage, payload: bytes) -> Message:
    """Wire payload -> an instance of the layout the route was subscribed with."""
    return message_class.from_bytes(BinaryReader(payload))
