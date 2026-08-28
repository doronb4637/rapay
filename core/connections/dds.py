"""
RTI Connext DDS connection.

DDS is data-centric and topic-based rather than socket/port based, but it still
honors the same JSON config / unit-routing contract as everything else. Two
things make that possible, and both are worth understanding before touching
this file:

  * A TOPIC is not a unit. On the framed protocols, inbound routing comes from
    the transport -- which socket a message arrived on identifies the peer. A
    DDS DataReader has no such property: it serves every publisher on its topic
    at once. So topics and units are separate axes here (`config.topics` and
    `config.connections`), and the sending unit is identified from the SAMPLE,
    by reading `header.source_unit` off it.

  * DDS puts no opcode on the wire -- the topic IS the message identity. But
    this framework routes on `(unit_code, opcode)`, so each topic is given a
    stable local surrogate opcode derived from its name
    (`tools.general.topic_opcode`, collision-checked in `config.parse_topics`).
    Nothing about it is transmitted. It exists so that `receive_message`,
    `handle_on_receive`, `@route` and `periodic_sending` work over DDS exactly
    as they do over TCP.

Payloads are handed to and from Connext's Python API natively: NO
(UnitCode,OpCode,DataLength) header is added or expected, `uses_irs_parser`
stays False, and `_do_send` takes a typed sample instance rather than bytes.

Requires the `rti.connextdds` package (RTI Connext Python API, Connext 6.1+).
If it isn't installed, importing this module raises ImportError, and
connections/__init__.py simply skips registering the "dds" protocol -- the rest
of the system works fine without RTI present.

------------------------------------------------------------------------
Where the type modules and QoS file fit
------------------------------------------------------------------------
DDS splits "what the data looks like" from "how the middleware behaves" about
it, and each half comes from its own place:

  * The TYPES answer WHAT. They are ordinary Python modules using the
    `rti.types` decorators -- there is no `.idl` text file and no rtiddsgen
    step:

        import rti.types as idl

        @idl.struct
        class Header:
            source_unit: idl.uint8 = 0
            destination_unit: idl.uint8 = 0

        @idl.struct
        class Track:
            # @idl.struct builds a dataclass, so a NESTED struct member needs
            # default_factory; `= Header()` raises "mutable default" on import.
            header: Header = field(default_factory=Header)
            x: float = 0.0

    `@idl.struct` builds the TypeSupport that Connext uses to serialize
    instances and to publish the type's definition during discovery. There is
    nothing to register with this framework: DDS carries the type on the wire
    and RTI does the matching itself. `config['idl_modules']` just names the
    modules to import so the classes exist.

  * The QoS (XML) file answers HOW. One universal file is the normal shape: it
    is a library of named profiles, and per-topic settings live INSIDE a
    profile as `topic_filter` attributes. That is why every QoS lookup below
    passes a topic name -- the profile-only accessors cannot see a
    `topic_filter` and would silently hand every topic the profile's base QoS.

Both are plain entries in the JSON config:

    {
      "protocol": "dds",
      "side": "publisher",
      "ip": "0.0.0.0",
      "unitCode": 22,
      "connections": {
        "RadarUnit": {"port": 0, "unitCode": 7}
      },
      "idl_modules": ["core.DDS.Structures.Example.example_topics"],
      "qos_file": "core/configs/qos/UNIVERSAL_QOS.xml",
      "qos_profile": "MyLib::Reliable",
      "topics": [
        {"topic": "TrackTopic",  "type": "Track",  "direction": "subscribe"},
        {"topic": "StatusTopic", "type": "Status", "direction": "publish"}
      ]
    }

`port` is the DDS domain id (one participant per connection, so every unit here
must agree on it). `ip` / `local_ip` are unused -- DDS addressing is discovery
and QoS, not endpoints.

The exact Connext calls, and the order they must happen in, are documented
inline in `_do_start()` below.

Echo is rejected at config load time for DDS (see `config.from_json`): the echo
lifecycle transmits raw bytes, which a DataWriter cannot accept. DDS LIVELINESS
QoS is the mechanism that belongs in that slot.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .base import Connection
from .config import ConnectionConfig, TopicSpec

logger = logging.getLogger("connmgr.dds")

import rti.connextdds as dds  # type: ignore  # raises ImportError if not installed
import rti.types as idl  # type: ignore
# Importing this is what ATTACHES `take_data_async` to dds.DataReader -- it is a
# monkey-patch at the bottom of rti/asyncio.py, not a method on the class. It
# looks unused to a linter and is load-bearing: without it `_read_loop` below
# raises AttributeError and no sample is ever received.
import rti.asyncio as rti_asyncio  # type: ignore

#: Default names for the routing fields inside a sample. Overridable per
#: connection through config['header'], because the layout is an ICD's
#: business, not ours.
DEFAULT_HEADER_FIELD = "header"
DEFAULT_SOURCE_FIELD = "source_unit"
DEFAULT_DESTINATION_FIELD = "destination_unit"

#: `rti.asyncio`'s waitset dispatcher is a PROCESS-wide singleton, and
#: `rti.asyncio.close()` tears it down for everyone. With two DdsConnections
#: live, one stopping must not blind the other, so the last one out closes it.
_live_connections = 0


class DdsConnection(Connection):
    """
    One DomainParticipant, one domain, and a DataWriter and/or DataReader per
    configured topic.

    Unit state is reported at `_do_start`: DDS discovery is asynchronous and
    peer-driven, so there is no handshake to wait on and no per-unit transport
    whose loss could be observed. Liveliness is DDS's own concern.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._participant: dds.DomainParticipant | None = None
        # Both keyed by TOPIC name: a topic's entities are shared by every unit
        # that speaks it, which is exactly why they cannot be keyed by unit.
        self._writers: dict[str, Any] = {}
        self._readers: dict[str, Any] = {}

        self._qos_provider: dds.QosProvider | None = None
        self._type_modules: list[ModuleType] = []
        self._closed_asyncio = False

        self._qos_file: str | None = config.extra.get("qos_file")
        self._qos_profile: str | None = config.extra.get("qos_profile")
        idl_modules = config.extra.get("idl_modules") or config.extra.get("idl_file") or []
        self._idl_modules: list[str] = [idl_modules] if isinstance(idl_modules, str) else list(idl_modules)

        header_cfg: dict[str, Any] = config.extra.get("header") or {}
        #: None means the routing fields sit at the top level of the sample
        #: rather than inside a nested struct.
        self._header_field: str | None = header_cfg.get("field", DEFAULT_HEADER_FIELD)
        self._source_field: str = header_cfg.get("source_unit", DEFAULT_SOURCE_FIELD)
        self._destination_field: str = header_cfg.get("destination_unit", DEFAULT_DESTINATION_FIELD)
        self._stamp_header: bool = bool(header_cfg.get("stamp", True))
        #: One warning per topic, not one per sample -- a mis-shaped header is a
        #: property of the type, so it would otherwise repeat at full data rate.
        self._header_warned: set[str] = set()

        if not config.topics:
            raise ValueError(
                "a 'dds' connection needs config['topics']: DDS has no port to listen on, so a "
                "topic list is the only thing that says what to publish or subscribe. "
                "e.g. [{'topic': 'TrackTopic', 'type': 'Track', 'direction': 'subscribe'}]")

        # Direction is a property of each topic, so the connection's own
        # capabilities are the union. This is what stops a subscribe-only DDS
        # connection being handed to `CompositeUnit` as a sender.
        self.can_send = any(topic.publishes for topic in config.topics)
        self.can_receive = any(topic.subscribes for topic in config.topics)

    @property
    def domain_id(self) -> int:
        """
        The DDS domain this connection joins.

        Every unit on one DdsConnection shares a single DomainParticipant, so
        they must share a domain: the first connection entry's port supplies
        it, and any entry disagreeing is a config error rather than a silent
        half-connected participant.
        """
        ports = self.config.ports
        if not ports:
            raise ValueError("DDS config has no connections; cannot determine a domain id")
        if len(set(ports)) > 1:
            raise ValueError(
                f"all units on one DDS connection must share a domain id, got ports={ports}; "
                f"split them into separate connections if they really are on different domains")
        return ports[0]

    # ------------------------------------------------------------------ #
    # Python type modules
    # ------------------------------------------------------------------ #
    async def _load_type_modules(self) -> list[ModuleType]:
        """
        Import every module named by `idl_modules`.

        A DDS type module registers nothing with this framework -- it just
        defines `@idl.struct` classes -- so this deliberately does NOT go
        through `tools.general.import_modules`, whose `_assert_registered`
        would reject every one of them for populating no IRS registry.

        Importing executes the module, which is why it runs in an executor:
        module-level work must never block the shared event-loop thread.
        """
        if not self._idl_modules:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._import_all, tuple(self._idl_modules))

    @staticmethod
    def _import_all(specs: tuple[str, ...]) -> list[ModuleType]:
        """Import each spec, by dotted name or by path (blocking; in an executor)."""
        modules: list[ModuleType] = []
        for spec in specs:
            path = Path(spec)
            if spec.endswith(".py") or path.is_file():
                modules.append(DdsConnection._import_from_path(path))
            else:
                # Dotted names get plain importlib: sys.modules caching is
                # already correct, and DDS type identity is per-class, so a
                # re-import would mint distinct types with the same name.
                modules.append(importlib.import_module(spec))
        return modules

    @staticmethod
    def _import_from_path(path: Path) -> ModuleType:
        """
        Import an out-of-tree type module by file path.

        The `sys.modules` key carries a digest of the RESOLVED path, not just
        the file stem: two different `tracks.py` under different directories
        are two different type modules, and keying on the stem alone would
        silently hand the second one the first one's classes.
        """
        resolved = path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"config['idl_modules'] entry not found: {resolved}")
        if resolved.suffix != ".py":
            raise ValueError(
                f"config['idl_modules'] entries must be Python modules defining @idl.struct "
                f"types, got {resolved.name}")
        digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
        module_name = f"_connmgr_dds_{resolved.stem}_{digest}"
        cached = sys.modules.get(module_name)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(module_name, resolved)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load a Python module from {resolved}")
        module = importlib.util.module_from_spec(spec)
        # Registered before exec_module so the module can be found by name
        # while its own body is still running.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)  # don't cache a half-built module
            raise
        logger.info("loaded DDS type module %s from %s", module_name, resolved)
        return module

    def _resolve_type(self, topic: TopicSpec) -> Any:
        """
        The `@idl.struct` class carrying `topic`.

        Preference order:
          1. `config.extra['type_resolver'](topic_name)` -- an escape hatch for
             callers holding a live type class this JSON-driven factory could
             never have imported itself.
          2. The class named by the topic's `type`, looked up across every
             imported type module.

        The result is checked to be an actual DDS type: `@idl.struct` attaches
        TypeSupport to the class, and `idl.get_type_support()` is what fails if
        the decorator was forgotten. Catching it here turns a confusing error
        from deep inside Connext's Topic constructor into one that names the
        class.
        """
        type_resolver = self.config.extra.get("type_resolver")
        if type_resolver is not None:
            return type_resolver(topic.topic)

        matches = [(module, getattr(module, topic.type_ref))
                   for module in self._type_modules if hasattr(module, topic.type_ref)]
        if not matches:
            available = sorted({
                name for module in self._type_modules for name in vars(module)
                if not name.startswith("_") and hasattr(getattr(module, name, None), "type_support")})
            raise ValueError(
                f"type {topic.type_ref!r} for topic {topic.topic!r} was not found in "
                f"config['idl_modules']={self._idl_modules}; DDS types available there: "
                f"{available or 'none -- is the module listed, and are its classes @idl.struct?'}")
        if len({type_cls for _module, type_cls in matches}) > 1:
            raise ValueError(
                f"type {topic.type_ref!r} for topic {topic.topic!r} is ambiguous: defined in "
                f"{[module.__name__ for module, _ in matches]}. DDS type identity is per-class, so "
                f"picking one arbitrarily would silently disagree with the other. Rename one, or "
                f"list only the module you meant in config['idl_modules'].")
        type_cls = matches[0][1]
        try:
            idl.get_type_support(type_cls)
        except Exception as exc:  # noqa: BLE001 - surfaced with actionable context
            raise TypeError(
                f"{topic.type_ref!r} (for topic {topic.topic!r}) is not a DDS type; decorate it "
                f"with @idl.struct (from `import rti.types as idl`)") from exc
        return type_cls

    # ------------------------------------------------------------------ #
    # QoS
    # ------------------------------------------------------------------ #
    def _load_qos_provider(self) -> dds.QosProvider | None:
        """
        `dds.QosProvider(url)` parses the QoS XML immediately and holds every
        `<qos_profile>` it declares. Nothing is applied yet -- a profile only
        takes effect when it is pulled out and passed to an entity's
        constructor, which is what `_qos_for()` below does.
        """
        if not self._qos_file:
            return None
        qos_path = Path(self._qos_file)
        if not qos_path.is_file():
            raise FileNotFoundError(f"config['qos_file'] not found: {qos_path.resolve()}")
        logger.info("loading DDS QoS profiles from %s", qos_path)
        return dds.QosProvider(str(qos_path))

    #: Per entity kind: the topic-aware accessor, then the profile-only one.
    _QOS_ACCESSORS: dict[str, tuple[str | None, str]] = {
        "participant": (None, "participant_qos"),
        "topic": ("set_topic_name_qos", "topic_qos"),
        "datawriter": ("set_topic_datawriter_qos", "datawriter_qos"),
        "datareader": ("set_topic_datareader_qos", "datareader_qos"),
    }

    def _qos_for(self, entity: str, topic_name: str | None = None) -> Any:
        """
        Fetch the QoS policy object for one entity kind, for one topic.

        The topic name matters. A universal QoS file carries its per-topic
        settings as `topic_filter` attributes INSIDE a profile, and only the
        topic-aware accessors evaluate those filters:

            set_topic_name_qos(profile, topic)       -> TopicQos
            set_topic_datawriter_qos(profile, topic) -> DataWriterQos
            set_topic_datareader_qos(profile, topic) -> DataReaderQos

        The profile-only accessors (`datawriter_qos_from_profile(profile)`) take
        no topic and therefore cannot see a `topic_filter` at all -- they would
        hand every topic the profile's base QoS and never say so. They are kept
        strictly as the fallback for a file that declares no filters.

        With no `qos_profile` configured we use the provider's default profile
        (the `datawriter_qos` / ... properties), which is the one marked
        `is_default_qos="true"` in the XML. With no QoS file at all we return
        None and let each constructor fall back to the DDS spec defaults
        (best-effort, volatile, ...).

        The returned object is applied at CONSTRUCTION time -- QoS is largely
        immutable once an entity exists, which is why every entity below is
        built with its QoS in hand rather than configured afterwards.
        """
        if self._qos_provider is None:
            return None
        topic_getter_name, default_property = self._QOS_ACCESSORS[entity]
        if topic_getter_name is not None and topic_name is not None and self._qos_profile:
            topic_getter = getattr(self._qos_provider, topic_getter_name, None)
            if topic_getter is not None:
                try:
                    return topic_getter(self._qos_profile, topic_name)
                except Exception as exc:  # noqa: BLE001 - older/simpler XML has no filters
                    logger.debug(
                        "%s(%r, %r) failed (%s); falling back to the profile-wide QoS",
                        topic_getter_name, self._qos_profile, topic_name, exc)
        if self._qos_profile:
            return getattr(self._qos_provider, f"{entity}_qos_from_profile")(self._qos_profile)
        return getattr(self._qos_provider, default_property)

    # ------------------------------------------------------------------ #
    # Header access
    # ------------------------------------------------------------------ #
    def _header_of(self, sample: Any) -> Any:
        """The struct carrying the routing fields, or None if this type has none."""
        if self._header_field is None:
            return sample
        return getattr(sample, self._header_field, None)

    def _header_value(self, sample: Any, field: str) -> Any:
        header = self._header_of(sample)
        return None if header is None else getattr(header, field, None)

    def _sending_unit(self, sample: Any, topic: TopicSpec) -> str | None:
        """
        Which configured unit sent `sample`, from its header.

        A DataReader serves every publisher on its topic, so this is the only
        thing that can tell two senders apart -- there is no per-peer socket to
        infer it from. A type that carries no header at all still works when the
        connection has exactly one configured unit, which is the common
        bring-up case and the one where the answer is unambiguous anyway.
        """
        source = self._header_value(sample, self._source_field)
        if source is None:
            units = self.config.connected_units
            if len(units) == 1:
                return units[0]
            if topic.topic not in self._header_warned:
                self._header_warned.add(topic.topic)
                logger.warning(
                    "topic %r: samples carry no %r field, and this connection has %d units (%s), "
                    "so the sender cannot be identified -- dropping. Set config['header'] to match "
                    "your type, or configure a single unit.",
                    topic.topic, self._source_field, len(units), units)
            return None
        return self.config.unit_for_code(int(source))

    def _stamp_outgoing(self, sample: Any, unit_name: str) -> None:
        """
        Fill in `source_unit` / `destination_unit` on an outgoing sample.

        The peer routes on these exactly as we do, so leaving them at zero is an
        easy and completely invisible mistake -- the sample goes out fine and is
        discarded at the far end. Values the caller set explicitly are never
        overwritten; only a field still at its zero default is filled.
        """
        if not self._stamp_header:
            return
        header = self._header_of(sample)
        if header is None:
            return
        if not getattr(header, self._source_field, None):
            try:
                setattr(header, self._source_field, self._own_unit_code)
            except Exception as exc:  # noqa: BLE001 - a read-only/absent field is not fatal
                logger.debug("could not stamp %r: %s", self._source_field, exc)
        if not getattr(header, self._destination_field, None):
            try:
                setattr(header, self._destination_field, self._unit_code_for(unit_name))
            except Exception as exc:  # noqa: BLE001
                logger.debug("could not stamp %r: %s", self._destination_field, exc)

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #
    def _validate_route(self, unit_name: str, route_key: tuple[int, int]) -> None:
        """
        DDS registers no IRS layouts, so the base implementation's `validate_irs`
        could never pass here. The question it asks is still the right one --
        "could this connection ever deliver this route?" -- so it is asked
        against the topic list instead: an opcode no configured topic routes on
        is a subscription that would block forever.
        """
        _unit_code, opcode = route_key
        if self.config.topic_for_opcode(opcode) is None:
            known = {topic.topic: f"{topic.opcode:#06x}" for topic in self.config.topics}
            raise ValueError(
                f"no DDS topic on this connection routes on opcode {opcode:#06x}; configured "
                f"topics and their opcodes are {known}. Opcodes are derived from the topic name "
                f"-- use tools.general.topic_opcode('<TopicName>') rather than a literal.")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def _do_start(self) -> None:
        global _live_connections

        # -- 1. Load the QoS file and the type modules up front. Neither touches
        #       the network; this is XML parsing and a Python import, so a typo
        #       fails fast and loudly before any DDS entity exists.
        self._qos_provider = self._load_qos_provider()
        self._type_modules = await self._load_type_modules()

        # -- 2. The DomainParticipant is the root entity and the unit of
        #       discovery: it joins the domain, starts the discovery endpoints,
        #       and owns every topic/reader/writer created under it. Its QoS
        #       (transports, discovery peers, buffer sizes) can only be set
        #       here, at construction.
        participant_qos = self._qos_for("participant")
        self._participant = (
            dds.DomainParticipant(self.domain_id, participant_qos)
            if participant_qos is not None
            else dds.DomainParticipant(self.domain_id))

        for topic_spec in self.config.topics:
            type_cls = self._resolve_type(topic_spec)

            # -- 3. Creating the Topic is what REGISTERS the type with the
            #       participant. Remote applications match on (topic name, type
            #       name, QoS compatibility), so this is the point where a
            #       Python class stops being a class and becomes half of a wire
            #       contract. `type_name` overrides the name the class is
            #       registered under, which is what a peer whose type came from
            #       real IDL will be advertising.
            topic = dds.Topic(
                self._participant,
                topic_spec.topic,
                type_cls,
                qos=self._qos_for("topic", topic_spec.topic),
                type_name=topic_spec.type_name,
            )

            # -- 4. Writers and readers carry the QoS that actually governs
            #       delivery (reliability, durability, history, deadline).
            #       These are the policies checked for RxO compatibility during
            #       discovery: mismatch them and the pair simply never connects
            #       -- no error, just silence.
            #       Unlike Topic, DataWriter/DataReader have no overload taking
            #       a None qos -- `qos` is a required positional on the
            #       (publisher, topic, qos) form -- so with no QoS file
            #       configured they must be built without the argument rather
            #       than with None, which would fail overload resolution.
            if topic_spec.publishes:
                writer_qos = self._qos_for("datawriter", topic_spec.topic)
                self._writers[topic_spec.topic] = (
                    dds.DataWriter(self._participant.implicit_publisher, topic, writer_qos)
                    if writer_qos is not None
                    else dds.DataWriter(self._participant.implicit_publisher, topic))
            if topic_spec.subscribes:
                reader_qos = self._qos_for("datareader", topic_spec.topic)
                reader = (
                    dds.DataReader(self._participant.implicit_subscriber, topic, reader_qos)
                    if reader_qos is not None
                    else dds.DataReader(self._participant.implicit_subscriber, topic))
                self._readers[topic_spec.topic] = reader
                self._track(self._read_loop(topic_spec, reader))

        _live_connections += 1
        self._closed_asyncio = False

        # Discovery is asynchronous and peer-driven, so there is no handshake to
        # wait on: a unit is usable here once the entities exist.
        for unit in self.config.connected_units:
            self._mark_unit_connected(unit)

    async def _read_loop(self, topic: TopicSpec, reader: Any) -> None:
        """
        Feed every inbound sample for `topic` into the framework's single
        dispatch point, tagged with the unit that sent it.

        `take_data_async` exists only because this module imports `rti.asyncio`
        (which monkey-patches it onto DataReader); its waitset dispatcher is
        created on first use, which is here -- on the shared loop thread, where
        it must be. `take_data` yields valid samples only, so the `valid_data`
        check callers would otherwise need is already done.
        """
        try:
            async for sample in reader.take_data_async():
                source = self._header_value(sample, self._source_field)
                # DDS delivers a participant's own writes back to its own
                # readers. Any connection that both publishes and subscribes a
                # topic therefore hears itself; the header says so plainly.
                if source is not None and int(source) == self._own_unit_code:
                    continue
                unit_name = self._sending_unit(sample, topic)
                if unit_name is None:
                    if source is not None:
                        # A third party on our topic is their business, not a
                        # fault of ours: warn once and keep reading.
                        key = f"{topic.topic}:{source}"
                        if key not in self._header_warned:
                            self._header_warned.add(key)
                            logger.warning(
                                "topic %r: sample from unconfigured unit code %s (configured: %s) "
                                "-- dropping", topic.topic, source, self.config.unit_codes)
                    continue
                self._dispatch_incoming(unit_name, topic.opcode, sample)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("DDS read loop failed for topic %s", topic.topic)

    async def _do_send(self, unit_name: str, data: Any, opcode: int) -> None:
        """
        Publish one sample on the topic that `opcode` routes to.

        `data` must be an instance of that topic's `@idl.struct` type, not
        bytes: DDS serializes through the type's TypeSupport, so there is
        nothing for this layer to frame. `opcode` is the topic's local surrogate
        (`tools.general.topic_opcode`) -- it selects the writer and is not
        transmitted.
        """
        topic = self.config.topic_for_opcode(opcode)
        if topic is None:
            known = {spec.topic: f"{spec.opcode:#06x}" for spec in self.config.topics}
            raise ValueError(
                f"no DDS topic routes on opcode {opcode:#06x}; configured topics are {known}")
        writer = self._writers.get(topic.topic)
        if writer is None:
            raise ConnectionError(
                f"topic {topic.topic!r} has no DataWriter on this connection: its direction is "
                f"{topic.direction.value!r}. Set it to 'publish' or 'both' to send on it.")
        if isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(
                f"DDS publishes typed samples, not bytes: topic {topic.topic!r} expects an "
                f"instance of {topic.type_ref}, got {type(data).__name__}.")
        self._stamp_outgoing(data, unit_name)
        writer.write(data)

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """
        Nothing to close for one unit.

        A DDS reader/writer belongs to a TOPIC and serves every unit speaking
        it, so closing entities here would cut off the other units too. There is
        also nothing that triggers this in practice: echo is rejected on DDS
        configs, so the watchdog that calls it never arms. The base class has
        already marked the unit disconnected by the time this runs.
        """
        logger.debug(
            "DDS unit %s marked disconnected; topic entities are shared and stay open", unit_name)

    async def _do_stop(self) -> None:
        """Close every DDS entity, innermost first: readers and writers before
        the participant that owns them."""
        global _live_connections

        for reader in self._readers.values():
            reader.close()
        for writer in self._writers.values():
            writer.close()
        if self._participant is not None:
            self._participant.close()
        self._readers.clear()
        self._writers.clear()
        self._participant = None
        self._qos_provider = None
        # Type modules are deliberately left in sys.modules: their classes carry
        # DDS type identity, and re-importing on a later start() would mint
        # distinct types with the same name.
        self._type_modules = []

        if not self._closed_asyncio:
            self._closed_asyncio = True
            _live_connections = max(0, _live_connections - 1)
            if _live_connections == 0:
                # Shared with every other DdsConnection in this process, so only
                # the last one may tear it down.
                await rti_asyncio.close()
