"""
The vocabulary a DDS Interface is written in.

A DDS Interface is a GENERATED Python module -- the system contract, rendered
from the system XML -- naming every unit in the system, its wire unit code, and
the topic classes it publishes and subscribes:

    from core.DDS import DdsUnit
    from my_icd.topics import Status, Track

    INTERFACE_FORMAT = 1
    SYSTEM = "ExampleSystem"

    SensorUnit = DdsUnit(unitCode=0x01, publish=(Track,), subscribe=(Status,))
    ControlUnit = DdsUnit(unitCode=0x02, publish=(Status,), subscribe=(Track,))

`DdsUnit` is the one piece of it that is not generated. Every Interface imports
it from here, and that shared class is what lets
`core.connections.dds_config` find the units in any Interface by type alone.

Pure Python on purpose: no `rti`, nothing else in this repo. `core.DDS` stays
importable without Connext, and the dependency arrow keeps pointing from
`core.connections` into `core.DDS`, never back.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: The Interface layout this code reads. A generated Interface states the layout
#: it was written for in its own `INTERFACE_FORMAT`, and the loader refuses any
#: other value rather than half-read a file whose meaning has moved on.
INTERFACE_FORMAT = 1


def _is_topic_type(cls: object) -> bool:
    """Whether `cls` can carry a topic: an `@idl.struct` or `@idl.union`.
    `@idl.enum`/`@idl.alias` classes have TypeSupport too, but cannot."""
    if not isinstance(cls, type):
        return False
    type_support = getattr(cls, "type_support", None)
    return bool(getattr(type_support, "is_valid_topic_type", False))


def _topic_classes(value: object, field: str) -> tuple[type, ...]:
    """
    One `publish`/`subscribe` argument as a tuple of topic classes.

    `publish=(Track)` is not a tuple -- without a trailing comma the parentheses
    only group -- so a lone class is taken as a one-element tuple instead of
    leaving a generator's punctuation to break the contract.
    """
    if isinstance(value, type):
        classes: tuple[object, ...] = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        classes = tuple(value)
    else:
        raise TypeError(f"DdsUnit.{field} must be a topic class or an iterable of them, got {value!r}")
    for cls in classes:
        if not _is_topic_type(cls):
            raise TypeError(
                f"DdsUnit.{field}: {cls!r} is not a DDS topic type -- decorate it with "
                f"@idl.struct (or @idl.union), from `import rti.types as idl`")
    repeated = sorted({cls.__name__ for cls in classes if classes.count(cls) > 1})
    if repeated:
        raise ValueError(f"DdsUnit.{field} lists {repeated} more than once")
    return classes  # type: ignore[return-value]  # every entry was just checked


@dataclass(frozen=True, slots=True)
class DdsUnit:
    """
    One unit of the system: its wire identity and the topics it may publish
    and subscribe.

    Two names are deliberately NOT fields, because each is already written once:
    the unit's name is the module-level variable this is bound to in the
    Interface (`SensorUnit = DdsUnit(...)`), and a topic's name is its class's
    wire type name (the class name unless pinned with `idl.type_name`).

    `unitCode` is the unit's system id: every sample carries its sender's as
    `A_sourceID.A_systemId`. A class may appear in both `publish` and `subscribe`.
    """

    unitCode: int
    publish: tuple[type, ...] = ()
    subscribe: tuple[type, ...] = ()

    def __post_init__(self) -> None:
        # Checked HERE, at the Interface's own import, so a bad generated line
        # fails pointing at that line rather than at whichever config loads it.
        if isinstance(self.unitCode, bool) or not isinstance(self.unitCode, int):
            raise TypeError(f"DdsUnit.unitCode must be an int, got {self.unitCode!r}")
        if not 0 <= self.unitCode <= 0xFF:
            raise ValueError(f"DdsUnit.unitCode = {self.unitCode} is not a uint8 unit code")
        object.__setattr__(self, "publish", _topic_classes(self.publish, "publish"))
        object.__setattr__(self, "subscribe", _topic_classes(self.subscribe, "subscribe"))
