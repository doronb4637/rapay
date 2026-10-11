"""
The vocabulary a DDS Interface is written in.

A DDS Interface is a GENERATED Python module -- the system contract, rendered
from the system XML -- naming every unit in the system, its wire unit code, and
the topic classes it publishes and subscribes:

    from core.DDS import DdsUnit
    from my_icd.topics import Status, Track

    SYSTEM = "ExampleSystem"

    SensorUnit = DdsUnit(unitCode=0x01, publish=(Track,), subscribe=(Status,))
    ControlUnit = DdsUnit(unitCode=0x02, publish=(Status,), subscribe=(Track,))

Pure Python on purpose: no `rti`, nothing else in this repo.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


def _as_tuple(value: type | Iterable[type]) -> tuple[type, ...]:
    # `publish=(Track)` is a bare class, not a tuple.
    return (value,) if isinstance(value, type) else tuple(value)


@dataclass(frozen=True, slots=True)
class DdsUnit:
    """
    One unit of the system. Its name is the variable it is bound to in the
    Interface; a topic's name is its class's wire type name.

    `unitCode` is what every sample carries as `A_sourceID.A_systemId`.
    """

    unitCode: int
    publish: tuple[type, ...] = ()
    subscribe: tuple[type, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "publish", _as_tuple(self.publish))
        object.__setattr__(self, "subscribe", _as_tuple(self.subscribe))
