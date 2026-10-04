"""
The DDS half of a connection's configuration: which DDS Interface a node runs
on, which unit it is in it, and the deployment settings (domain, QoS) that are
not part of the system contract.

A DDS Interface is a generated module written in `core.DDS.interface`'s
vocabulary (reference instance: `core/DDS/Interfaces/Example/example_interface.py`):

    from core.DDS import DdsUnit
    from my_icd.topics import Status, Track

    INTERFACE_FORMAT = 1
    SYSTEM = "ExampleSystem"            # optional

    SensorUnit = DdsUnit(unitCode=0x01, publish=(Track,), subscribe=(Status,))
    ControlUnit = DdsUnit(unitCode=0x02, publish=(Status,), subscribe=(Track,))

Two naming rules keep it that small, and both are load-bearing:

  * A unit's name is the variable it is bound to: `"unit": "SensorUnit"` in a
    config picks `SensorUnit` above.
  * A topic's name is its class's wire type name (`type_support.type_name`):
    the class name, unless the type pins one with `idl.type_name`, as IDL with
    modules does (`P_Radar_PSM::Track`). There is no topic table -- the classes
    ARE the topics -- so the units' publish/subscribe lists are the whole
    routing contract.

Everything here runs at CONFIG LOAD (`ConnectionConfig.from_json` calls
`resolve_unit`), so a contract that cannot work fails `create()`, never
`start()`. This module imports no `rti` itself; loading an Interface pulls in
Connext only because the Interface imports its topic classes.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

from core.annotations import UnitCode
from core.DDS.interface import INTERFACE_FORMAT, DdsUnit
from core.tools.general import names_a_file

logger = logging.getLogger("connmgr.dds")

DEFAULT_DOMAIN_ID: int = 67
_CORE_DIR: Path = Path(__file__).resolve().parent.parent
DEFAULT_QOS_FILE: Path = _CORE_DIR / "DDS" / "Configuration" / "UNIVERSAL_QOS.xml"
DEFAULT_LICENSE_FILE: Path = _CORE_DIR / "rti_license.dat"


class TopicDirection(str, Enum):
    """Which entities this unit gets for a topic: a writer, a reader, or both."""
    PUBLISH = "publish"
    SUBSCRIBE = "subscribe"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class TopicSpec:
    """
    `publishers` / `subscribers` are the PEERS the Interface lists for the topic
    -- never this unit itself. They are what attributes a sample that carries no
    header, what a send's destination is checked against, and what lets
    `unit_name` be left out when only one peer speaks the topic.
    """

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

    @property
    def type_name(self) -> str:
        """The DDS type name registered on the wire: the class name, unless the
        type pins another with `@idl.struct(type_annotations=[idl.type_name(...)])`."""
        return self.sample_type.type_support.type_name


def _qualified(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def topic_name_of(cls: type) -> str:
    """The topic `cls` carries: its wire type name. Falls back to the class name
    for a selector that is not an @idl type at all."""
    type_support = getattr(cls, "type_support", None)
    return cls.__name__ if type_support is None else type_support.type_name


@dataclass(frozen=True, slots=True)
class DdsUnitConfig:
    """
    *Immutable class*
    This connection's unit, as the DDS Interface defines it, plus the
    deployment settings around it. Built by `resolve_unit` at config load.
    """

    interface: str
    system: str
    unit: str
    unit_code: UnitCode
    #: (peer name, peer unit code), in the Interface's declaration order.
    peers: tuple[tuple[str, UnitCode], ...]
    #: Only the topics THIS unit publishes or subscribes -- the entity plan.
    topics: tuple[TopicSpec, ...]
    domain_id: int = DEFAULT_DOMAIN_ID
    qos_file: Path = DEFAULT_QOS_FILE
    #: None means the QoS file's default (`is_default_qos="true"`) profile.
    qos_profile: str | None = None

    @property
    def topic_names(self) -> list[str]:
        return [spec.name for spec in self.topics]

    def topic_named(self, name: str) -> TopicSpec | None:
        for spec in self.topics:
            if spec.name == name:
                return spec
        return None

    def topic_for(self, selector: Any) -> TopicSpec:
        """
        The topic a caller means: a topic class, a sample of one, or a topic
        name -- the DDS spelling of what the framed protocols call an opcode.

        A class whose name matches a topic but which is a DIFFERENT class object
        gets its own error: that is two copies of one generated module loaded
        side by side, and reporting it as "unknown topic" would hide the cause.
        """
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
        spec = self.topic_named(topic_name_of(cls))
        if spec is not None:
            raise TypeError(second_copy_message(cls, spec))
        raise ValueError(
            f"unit {self.unit!r} has no topic carried by {_qualified(cls)}; its topics are "
            f"{self.topic_names}")


def second_copy_message(cls: type, spec: TopicSpec) -> str:
    """Why a class named like a topic is still not that topic's class."""
    return (
        f"{_qualified(cls)} is not the class topic {spec.name!r} is carried by "
        f"({_qualified(spec.sample_type)}): two copies of one generated module are loaded. "
        f"Build samples from the same import the DDS Interface uses -- import the topic "
        f"classes by absolute module name in both places.")


# --------------------------------------------------------------------------- #
# Loading an Interface
# --------------------------------------------------------------------------- #
def load_dds_interface(spec: str | Path) -> ModuleType:
    """
    Import a DDS Interface by `.py` path (the normal case) or dotted name.

    A path is imported under a `sys.modules` name carrying a digest of the
    RESOLVED path, so one file always comes back as one module object -- the
    identity its topic classes depend on -- and two files that merely share a
    stem never collide. Application code that needs the Interface module itself
    should come through here rather than importing the file some other way.
    """
    text = str(spec)
    if not names_a_file(text):
        try:
            return importlib.import_module(text)
        except ImportError as exc:
            explained = _explained(exc, text)
            if explained is exc:
                raise
            raise explained from exc
    path = Path(text).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"DDS Interface not found: {path}")
    if path.suffix != ".py":
        raise ValueError(f"a DDS Interface is a generated Python module, got {path.name}")
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    module_name = f"_connmgr_dds_interface_{path.stem}_{digest}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"cannot load a Python module from {path}")
    module = importlib.util.module_from_spec(module_spec)
    # Registered before exec_module so the module can be found by name while
    # its own body runs; dropped again on failure so a retry re-executes.
    sys.modules[module_name] = module
    try:
        module_spec.loader.exec_module(module)
    except ImportError as exc:
        sys.modules.pop(module_name, None)
        explained = _explained(exc, str(path))
        if explained is exc:
            raise
        raise explained from exc
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    logger.info("loaded DDS Interface %s from %s", module_name, path)
    return module


def _explained(exc: ImportError, where: str) -> ImportError:
    """The two import failures an Interface author can actually act on, said
    plainly; anything else comes back unchanged."""
    if "relative import" in str(exc):
        return ImportError(
            f"DDS Interface {where} uses a relative import ({exc}). An Interface loaded by path "
            f"has no parent package, and giving it one would load a private second copy of its "
            f"topic classes -- not the ones your code builds samples from. Import DdsUnit and "
            f"the topic classes by absolute module name.")
    if (exc.name or "").split(".")[0] == "rti":
        return ImportError(
            f"DDS Interface {where} needs the RTI Connext Python API (rti.connext), which is not "
            f"installed: {exc}")
    return exc


def _units_of(module: ModuleType, where: str) -> dict[str, DdsUnit]:
    """Every module-level `DdsUnit`, by variable name, in declaration order --
    with one object per name and one name per unit code."""
    units: dict[str, DdsUnit] = {}
    name_of: dict[int, str] = {}
    code_owner: dict[int, str] = {}
    for name, value in vars(module).items():
        if not isinstance(value, DdsUnit):
            continue
        if id(value) in name_of:
            raise ValueError(
                f"{where}: {name_of[id(value)]!r} and {name!r} are the same DdsUnit; a unit is "
                f"named by exactly one variable")
        name_of[id(value)] = name
        if value.unitCode in code_owner:
            raise ValueError(
                f"{where}: units {code_owner[value.unitCode]!r} and {name!r} both use unitCode "
                f"{value.unitCode:#04x}; unit codes identify senders, so they must be unique")
        code_owner[value.unitCode] = name
        units[name] = value
    if not units:
        lookalike = getattr(module, "DdsUnit", None)
        hint = (" It defines its own DdsUnit class -- import the shared one instead: "
                "`from core.DDS import DdsUnit`."
                if isinstance(lookalike, type) and lookalike is not DdsUnit else "")
        raise ValueError(f"{where} defines no DdsUnit.{hint}")
    return units


def _check_topic_names(units: dict[str, DdsUnit], where: str) -> None:
    """One class per topic name. Two classes sharing a type name would be one
    topic on the wire carried by two different types -- or the same generated
    module imported twice."""
    owner: dict[str, type] = {}
    for unit in units.values():
        for cls in (*unit.publish, *unit.subscribe):
            name = topic_name_of(cls)
            known = owner.setdefault(name, cls)
            if known is not cls:
                raise ValueError(
                    f"{where}: topic {name!r} is carried by two different classes, "
                    f"{_qualified(known)} and {_qualified(cls)}. A topic's name is its type "
                    f"name, so these would collide on the wire; if they are the same generated "
                    f"module, import it one way only.")


def _own_topics(unit: str, units: dict[str, DdsUnit]) -> tuple[TopicSpec, ...]:
    own = units[unit]
    others = {name: other for name, other in units.items() if name != unit}
    specs: list[TopicSpec] = []
    for cls in dict.fromkeys((*own.publish, *own.subscribe)):
        publishes, subscribes = cls in own.publish, cls in own.subscribe
        direction = (TopicDirection.BOTH if publishes and subscribes
                     else TopicDirection.PUBLISH if publishes else TopicDirection.SUBSCRIBE)
        specs.append(TopicSpec(
            name=topic_name_of(cls),
            sample_type=cls,
            direction=direction,
            publishers=tuple(name for name, other in others.items() if cls in other.publish),
            subscribers=tuple(name for name, other in others.items() if cls in other.subscribe),
        ))
    return tuple(specs)


def resolve_unit(interface: str, unit: str, *, domain_id: int = DEFAULT_DOMAIN_ID,
                 qos_file: Path = DEFAULT_QOS_FILE, qos_profile: str | None = None) -> DdsUnitConfig:
    """
    Load `interface`, validate ALL of it, and return `unit`'s view of it.

    The whole Interface is checked, not just this unit's slice: a contract that
    is broken for one unit is broken for every unit that loads it, and the
    sooner a generator bug surfaces the closer it is to its cause.
    """
    module = load_dds_interface(interface)
    where = f"DDS Interface {interface!r}"
    declared = getattr(module, "INTERFACE_FORMAT", None)
    if declared != INTERFACE_FORMAT:
        missing = " (the generated file must set it)" if declared is None else ""
        raise ValueError(
            f"{where} declares INTERFACE_FORMAT = {declared!r}{missing}; this code reads format "
            f"{INTERFACE_FORMAT}")
    units = _units_of(module, where)
    _check_topic_names(units, where)
    if unit not in units:
        raise ValueError(f"{where} has no unit {unit!r}; its units are {list(units)}")

    topics = _own_topics(unit, units)
    peer_names: dict[str, None] = {}
    for spec in topics:
        if spec.subscribes:
            peer_names.update(dict.fromkeys(spec.publishers))
        if spec.publishes:
            peer_names.update(dict.fromkeys(spec.subscribers))
    if not peer_names:
        raise ValueError(
            f"{where}: unit {unit!r} has no peers -- no other unit publishes what it subscribes "
            f"or subscribes what it publishes -- so there is nothing for it to talk to")
    for spec in topics:
        if spec.subscribes and not spec.publishers:
            logger.warning("%s: %s subscribes %r, but no other unit publishes it", where, unit, spec.name)
        if spec.publishes and not spec.subscribers:
            logger.warning("%s: %s publishes %r, but no other unit subscribes it", where, unit, spec.name)

    return DdsUnitConfig(
        interface=str(interface),
        system=str(getattr(module, "SYSTEM", None) or module.__name__),
        unit=unit,
        unit_code=units[unit].unitCode,
        # Declaration order, not discovery order, so logs and errors are stable.
        peers=tuple((name, units[name].unitCode) for name in units if name in peer_names),
        topics=topics,
        domain_id=domain_id,
        qos_file=qos_file,
        qos_profile=qos_profile,
    )
