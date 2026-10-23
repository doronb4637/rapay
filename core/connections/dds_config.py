"""
The DDS half of a connection's configuration: which DDS Interface a node runs
on, which unit it is in it, and the deployment settings (domain, QoS) that are
not part of the system contract.

A DDS Interface is a generated module written in `core.DDS.interface`'s
vocabulary (reference instance: `core/DDS/Interfaces/example_interface.py`):

    from core.DDS import DdsUnit
    from ..pyIdl.topics import Status, Track

    SYSTEM = "ExampleSystem"            # optional

    SensorUnit = DdsUnit(unitCode=0x01, publish=(Track,), subscribe=(Status,))
    ControlUnit = DdsUnit(unitCode=0x02, publish=(Status,), subscribe=(Track,))

A unit's name is the variable it is bound to; a topic's name is its class's wire
type name (`type_support.type_name`, e.g. `P_Radar_PSM::Track`). The Interface
is generated and trusted: nothing here validates it.

Runs at CONFIG LOAD (`ConnectionConfig.from_json` calls `resolve_unit`). This
module imports no `rti` itself; loading an Interface pulls in Connext only
because the Interface imports its topic classes.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

from core.annotations import UnitCode
from core.DDS.interface import DdsUnit
from core.tools.general import import_modules, names_a_file

DEFAULT_DOMAIN_ID: int = 50
_CORE_DIR: Path = Path(__file__).resolve().parent.parent
DEFAULT_QOS_FILE: Path = _CORE_DIR / "DDS" / "Configuration" / "UNIVERSAL_QOS.xml"
DEFAULT_LICENSE_FILE: Path = _CORE_DIR / "rti_license.dat"


class TopicDirection(str, Enum):
    """What this unit does with a topic."""
    PUBLISH = "publish"
    SUBSCRIBE = "subscribe"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class TopicSpec:
    """`publishers` / `subscribers` are the PEERS the Interface lists for the
    topic -- never this unit itself."""

    name: str
    sample_type: type
    direction: TopicDirection
    publishers: tuple[str, ...] = ()
    subscribers: tuple[str, ...] = ()

    @property
    def publishes(self) -> bool:
        return self.direction in (TopicDirection.PUBLISH, TopicDirection.BOTH)

    @property
    def subscribes(self) -> bool:
        return self.direction in (TopicDirection.SUBSCRIBE, TopicDirection.BOTH)


def _qualified(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


@dataclass(frozen=True, slots=True)
class DdsUnitConfig:
    """This connection's unit, as the DDS Interface defines it, plus the
    deployment settings around it. Built by `resolve_unit` at config load."""

    interface: str
    system: str
    unit: str
    unit_code: UnitCode
    #: (peer name, peer unit code)
    peers: tuple[tuple[str, UnitCode], ...]
    topics: tuple[TopicSpec, ...]
    domain_id: int = DEFAULT_DOMAIN_ID
    qos_file: Path = DEFAULT_QOS_FILE
    #: None means the QoS file's default (`is_default_qos="true"`) profile.
    qos_profile: str | None = None

    @property
    def topic_names(self) -> list[str]:
        return [spec.name for spec in self.topics]

    def topic_named(self, name: str) -> TopicSpec | None:
        """The topic called `name`, or None."""
        for spec in self.topics:
            if spec.name == name:
                return spec
        return None

    def topic_for(self, selector: Any) -> TopicSpec:
        """The topic a caller means: a topic class, a sample of one, or a topic
        name -- the DDS spelling of what the framed protocols call an opcode."""
        if selector is None:
            raise TypeError(
                f"a DDS topic is required: pass its class, a sample of it, or its name "
                f"({self.topic_names})")
        if isinstance(selector, str):
            spec = self.topic_named(selector)
            if spec is None:
                raise ValueError(
                    f"unit {self.unit!r} has no topic {selector!r}; its topics are {self.topic_names}")
            return spec
        if isinstance(selector, int):
            raise TypeError(
                f"DDS has no opcodes: select a topic by its class, a sample of it, or its name "
                f"({self.topic_names}), got {selector!r}")
        cls = selector if isinstance(selector, type) else type(selector)
        for spec in self.topics:
            if spec.sample_type is cls:
                return spec
        # Qualified on both sides: a class matching by name but not by module is
        # a second copy of the generated module.
        raise ValueError(
            f"unit {self.unit!r} has no topic carried by {_qualified(cls)}; its topic classes "
            f"are {[_qualified(spec.sample_type) for spec in self.topics]}")


def load_dds_interface(spec: str | Path) -> ModuleType:
    """
    Import a DDS Interface by dotted name or `.py` path.

    Name it dotted, spelled like the application's own `import`, when the
    application imports its topic classes; name it by path only when nothing
    else imports it (gsim). A path load of a file already imported under a real
    name raises `ImportError` rather than mint a second copy of every class.

    Loaded through the same `import_modules` as structures files: a path is
    imported under a name derived from its RESOLVED path, so one file always
    comes back as one module object -- the identity its topic classes depend
    on -- and every real folder is a package, so an Interface kept anywhere on
    disk can import its topics relatively (`from ..pyIdl.topics import Track`).
    """
    text = str(spec)
    if names_a_file(text) and not Path(text).resolve().is_file():
        raise FileNotFoundError(f"DDS Interface not found: {Path(text).resolve()}")
    return import_modules(text)[0]


def _direction(publishes: bool, subscribes: bool) -> TopicDirection:
    if publishes and subscribes:
        return TopicDirection.BOTH
    return TopicDirection.PUBLISH if publishes else TopicDirection.SUBSCRIBE


def resolve_unit(interface: str, unit: str, *, domain_id: int = DEFAULT_DOMAIN_ID,
                 qos_file: Path = DEFAULT_QOS_FILE, qos_profile: str | None = None) -> DdsUnitConfig:
    """
    Load the DDS Interface at `interface` (a `.py` path or dotted name) and
    return `unit`'s view of it: its code, topics, and the peers at the other end.

    `domain_id`, `qos_file` and `qos_profile` are deployment settings and are
    carried through unchanged. Raises `FileNotFoundError` for a missing
    Interface file and `ValueError` if the Interface has no unit named `unit`.
    """
    module = load_dds_interface(interface)
    units: dict[str, DdsUnit] = {
        name: value for name, value in vars(module).items() if type(value).__name__ == "DdsUnit"}
    if unit not in units:
        raise ValueError(f"DDS Interface {interface!r} has no unit {unit!r}; its units are {list(units)}")
    own = units[unit]
    others = {name: other for name, other in units.items() if name != unit}

    topics = tuple(
        TopicSpec(
            name=cls.type_support.type_name,
            sample_type=cls,
            direction=_direction(cls in own.publish, cls in own.subscribe),
            publishers=tuple(name for name, other in others.items() if cls in other.publish),
            subscribers=tuple(name for name, other in others.items() if cls in other.subscribe),
        )
        for cls in dict.fromkeys((*own.publish, *own.subscribe)))
    # A peer publishes something we subscribe, or subscribes something we publish.
    peers = tuple(
        (name, other.unitCode) for name, other in others.items()
        if any(cls in other.publish for cls in own.subscribe)
        or any(cls in other.subscribe for cls in own.publish))

    return DdsUnitConfig(
        interface=str(interface),
        system=str(getattr(module, "SYSTEM", None) or module.__name__),
        unit=unit,
        unit_code=own.unitCode,
        peers=peers,
        topics=topics,
        domain_id=domain_id,
        qos_file=qos_file,
        qos_profile=qos_profile,
    )
