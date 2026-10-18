"""
The DDS half of a connection's configuration: which DDS Interface a node runs
on, which unit it is in it, and the deployment settings (domain, QoS) that are
not part of the system contract.

A DDS Interface is a generated module written in `core.DDS.interface`'s
vocabulary (reference instance: `core/DDS/Interfaces/Example/example_interface.py`):

    from core.DDS import DdsUnit
    from my_icd.topics import Status, Track

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

import hashlib
import importlib
import importlib.util
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

from core.annotations import UnitCode
from core.DDS.interface import DdsUnit
from core.tools.general import names_a_file

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
    Import a DDS Interface by `.py` path (the normal case) or dotted name.

    A path is imported under a `sys.modules` name carrying a digest of the
    RESOLVED path, so one file always comes back as one module object -- the
    identity its topic classes depend on -- and two files that merely share a
    stem never collide.
    """
    text = str(spec)
    if not names_a_file(text):
        return importlib.import_module(text)
    path = Path(text).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"DDS Interface not found: {path}")
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    module_name = f"_connmgr_dds_interface_{path.stem}_{digest}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(module_spec)
    # Registered before exec_module so the module can be found by name while
    # its own body runs; dropped again on failure so a retry re-executes.
    sys.modules[module_name] = module
    try:
        module_spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[module_name]
        raise
    return module


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
