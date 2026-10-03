"""
RTI Connext DDS connection.

A DDS node is configured as ONE UNIT of a DDS Interface -- the generated module
that is the system contract (`core/DDS/interface.py`, `dds_config.py`) -- and
everything else follows from that:

  * Entities. A DataWriter for each topic the unit publishes, a DataReader for
    each topic it subscribes, and nothing else. A topic's name is its class's
    name.
  * Routing. DDS puts the topic, not an opcode, on the wire, so routes here are
    keyed by TOPIC NAME. Wherever the framed protocols take an opcode, callers
    name a topic by its class, a sample of it, or its name:

        unit.send_message(Track(track_id=1))        # every subscriber of Track
        status = unit.receive_message(Status, timeout=5)
        unit.handle_on_receive(Status, on_status)   # or @route(Status) on a UnitHandler

  * Senders. A DataReader serves every publisher of its topic at once, so the
    sending unit is read off the SAMPLE (`header.source_unit`), falling back to
    the Interface when it lists exactly one publisher of the topic.
  * Lifecycle. `participant.close()` closes every topic, writer and reader the
    participant contains. The one ordering that is ours to get right is
    stopping the read loops before it (see `_close_entities`).

Payloads are native: no (UnitCode,OpCode,DataLength) header, `uses_irs_parser`
stays False, and `_do_send` takes a typed sample rather than bytes.

Configuration -- only `unit` and `dds_interface` are required:

    {
      "protocol": "dds",
      "unit": "SensorUnit",
      "dds_interface": "C:/ICD/generated/dds_interface.py",
      "domain_id": 0,                     # default dds_config.DEFAULT_DOMAIN_ID
      "qos_file": ".../UNIVERSAL_QOS.xml",  # default dds_config.DEFAULT_QOS_FILE
      "qos_profile": "MyLib::Reliable",   # default: the file's is_default_qos profile
      "header": {"field": "header", "source_unit": "source_unit",
                 "destination_unit": "destination_unit", "stamp": true}
    }

QoS answers HOW, from one universal file of named profiles. Per-topic settings
live inside a profile as `topic_filter` attributes, which is why every entity
QoS lookup passes the topic name (`_qos_for`).

Requires `rti.connextdds` (Connext 6.1+). Without it, importing this module
raises ImportError and connections/__init__.py skips registering "dds".
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import rti.connextdds as dds  # type: ignore  # raises ImportError if not installed
# Importing this is what ATTACHES `take_data_async` to dds.DataReader -- it is a
# monkey-patch at the bottom of rti/asyncio.py, not a method on the class. It
# looks unused to a linter and is load-bearing: without it `_read_loop` below
# raises AttributeError and no sample is ever received.
import rti.asyncio as rti_asyncio  # type: ignore

from ._routes import MessageKey, RouteKey, UnitName
from .base import Connection, OpCode
from .config import ConnectionConfig
from .dds_config import DdsUnitConfig, TopicDirection, TopicSpec

logger = logging.getLogger("connmgr.dds")

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
    One DomainParticipant on one domain, with a DataWriter / DataReader for
    exactly the topics this unit's DdsUnit publishes / subscribes.

    Peers are marked connected at `_do_start`: DDS discovery is asynchronous and
    peer-driven, so there is no handshake to wait on and no per-unit transport
    whose loss could be observed. Liveliness is DDS's own concern.
    """

    def __init__(self, config: ConnectionConfig) -> None:
        if config.dds is None:
            raise ValueError(
                "DdsConnection needs a protocol 'dds' config -- ConnectionConfig.from_json of "
                "{'protocol': 'dds', 'unit': ..., 'dds_interface': ...} -- but config.dds is None")
        super().__init__(config)
        self._dds: DdsUnitConfig = config.dds
        self._participant: dds.DomainParticipant | None = None
        # Keyed by TOPIC name: a topic's entities serve every peer that speaks it.
        self._writers: dict[str, Any] = {}
        self._readers: dict[str, Any] = {}
        #: The read loops, held apart from `_tasks` because they must stop
        #: BEFORE the participant closes, not after (see `_close_entities`).
        self._read_tasks: list[asyncio.Task[None]] = []
        self._counted_live = False

        header_cfg: dict[str, Any] = config.extra.get("header") or {}
        #: None means the routing fields sit at the top level of the sample
        #: rather than inside a nested struct.
        self._header_field: str | None = header_cfg.get("field", DEFAULT_HEADER_FIELD)
        self._source_field: str = header_cfg.get("source_unit", DEFAULT_SOURCE_FIELD)
        self._destination_field: str = header_cfg.get("destination_unit", DEFAULT_DESTINATION_FIELD)
        self._stamp_header: bool = bool(header_cfg.get("stamp", True))
        #: Warnings about inbound traffic are once per cause, not once per
        #: sample: a cause is a property of a peer or a type, so it would
        #: otherwise repeat at full data rate.
        self._warned: set[tuple[Any, ...]] = set()

        # Direction is per topic, so the connection's capabilities are the
        # union. This is what stops a subscribe-only unit being handed to
        # `CompositeUnit` as a sender.
        self.can_send = any(spec.publishes for spec in self._dds.topics)
        self.can_receive = any(spec.subscribes for spec in self._dds.topics)

        # Both in the caller's thread, so a bad QoS file/profile or a topic whose
        # senders cannot be told apart fails create(), not start().
        self._qos_provider: dds.QosProvider = self._load_qos_provider()
        self._check_senders_identifiable()

        logger.info(
            "DDS unit %s (unitCode %#04x) of %s: publishes %s, subscribes %s; peers %s; "
            "domain %d; QoS %s [%s]",
            self._dds.unit, self._dds.unit_code, self._dds.system,
            [spec.name for spec in self._dds.topics if spec.publishes],
            [spec.name for spec in self._dds.topics if spec.subscribes],
            [name for name, _code in self._dds.peers], self._dds.domain_id,
            self._dds.qos_file.name, self._dds.qos_profile or "default profile")

    # ------------------------------------------------------------------ #
    # QoS
    # ------------------------------------------------------------------ #
    def _load_qos_provider(self) -> dds.QosProvider:
        """
        Parse the QoS file. Nothing is applied yet: a profile only takes effect
        when it is pulled out and handed to an entity's constructor (`_qos_for`).
        A named profile is checked here so a typo fails at load.
        """
        provider = dds.QosProvider(str(self._dds.qos_file))
        profile = self._dds.qos_profile
        if profile is not None:
            try:
                provider.participant_qos_from_profile(profile)
            except Exception as exc:  # noqa: BLE001 - surfaced with the file and the profiles it has
                raise ValueError(
                    f"config['qos_profile'] = {profile!r} is not a profile in {self._dds.qos_file}; "
                    f"it defines {self._profiles_in(provider)}, besides RTI's Builtin* "
                    f"libraries") from exc
        return provider

    @staticmethod
    def _profiles_in(provider: dds.QosProvider) -> list[str]:
        """The file's own profiles -- RTI's ~130 built-in ones would bury them."""
        try:
            return [f"{library}::{profile}" for library in provider.qos_profile_libraries
                    if not library.startswith("Builtin")
                    for profile in provider.qos_profiles(library)]
        except Exception:  # noqa: BLE001 - only ever used to word an error
            return []

    #: Per entity kind: the accessor taking (profile, topic), and the one taking
    #: only (topic) for the file's default profile. Both evaluate the profile's
    #: `topic_filter`s against the topic name. The profile-only accessors
    #: (`datawriter_qos_from_profile`, `.datawriter_qos`) take no topic, so they
    #: cannot see a filter and would silently hand every topic the baseline.
    _QOS_ACCESSORS: dict[str, tuple[str, str]] = {
        "topic": ("set_topic_name_qos", "get_topic_name_qos"),
        "datawriter": ("set_topic_datawriter_qos", "get_topic_datawriter_qos"),
        "datareader": ("set_topic_datareader_qos", "get_topic_datareader_qos"),
    }

    def _qos_for(self, entity: str, topic_name: str) -> Any:
        """
        One entity kind's QoS for one topic. Applied at CONSTRUCTION: QoS is
        largely immutable once an entity exists, and writer/reader QoS is what
        RxO matching compares -- mismatch it and the pair never connects, with
        no error, just silence.
        """
        with_profile, default_profile = self._QOS_ACCESSORS[entity]
        profile = self._dds.qos_profile
        if profile:
            return getattr(self._qos_provider, with_profile)(profile, topic_name)
        return getattr(self._qos_provider, default_profile)(topic_name)

    def _participant_qos(self) -> Any:
        profile = self._dds.qos_profile
        if profile:
            return self._qos_provider.participant_qos_from_profile(profile)
        return self._qos_provider.participant_qos

    # ------------------------------------------------------------------ #
    # Header access
    # ------------------------------------------------------------------ #
    @property
    def _source_path(self) -> str:
        return self._source_field if self._header_field is None else f"{self._header_field}.{self._source_field}"

    def _header_of(self, sample: Any) -> Any:
        """The struct carrying the routing fields, or None if this type has none."""
        if self._header_field is None:
            return sample
        return getattr(sample, self._header_field, None)

    def _header_value(self, sample: Any, field: str) -> Any:
        header = self._header_of(sample)
        return None if header is None else getattr(header, field, None)

    def _carries_source(self, sample_type: type) -> bool | None:
        """Whether `sample_type` has the source field, or None if a default
        sample cannot be built to look."""
        try:
            sample = sample_type()
        except Exception:  # noqa: BLE001 - a type we cannot probe is not a type we can fault
            return None
        header = self._header_of(sample)
        return header is not None and hasattr(header, self._source_field)

    def _check_senders_identifiable(self) -> None:
        """
        A subscribed topic needs the header when anything but one peer can
        write to it: when several units publish it, or when this unit publishes
        it too (a participant's reader hears its own writer). Without the header
        every such sample would be unattributable and silently dropped, so it is
        a load error instead.
        """
        for spec in self._dds.topics:
            hears_itself = spec.publishes
            if not spec.subscribes or (len(spec.publishers) < 2 and not hears_itself):
                continue
            if self._carries_source(spec.sample_type) is False:
                why = ("also publishes it, and a participant hears its own writes" if hears_itself
                       else f"it has {len(spec.publishers)} publishers {list(spec.publishers)}")
                raise ValueError(
                    f"unit {self._dds.unit!r} subscribes {spec.name!r} and {why}, but "
                    f"{spec.sample_type.__qualname__} has no {self._source_path!r} field to tell "
                    f"the senders apart. Add the header to the type, or point config['header'] "
                    f"at the fields it does carry.")

    def _stamp_outgoing(self, sample: Any, unit_name: UnitName | None) -> None:
        """
        Fill in `source_unit` (and `destination_unit`, when the send named one)
        on an outgoing sample. The far end identifies the sender by the source,
        so leaving it at zero silently gets the sample dropped there. Only
        fields still at their zero default are filled; caller-set values stay.
        """
        if not self._stamp_header:
            return
        header = self._header_of(sample)
        if header is None:
            return
        stamps = [(self._source_field, self._own_unit_code)]
        if unit_name is not None:
            stamps.append((self._destination_field, self._unit_code_for(unit_name)))
        for field, code in stamps:
            if getattr(header, field, None):
                continue
            try:
                setattr(header, field, code)
            except Exception as exc:  # noqa: BLE001 - a read-only/absent field is not fatal
                logger.debug("could not stamp %r: %s", field, exc)

    def _warn_once(self, cause: tuple[Any, ...], message: str, *args: Any) -> None:
        if cause not in self._warned:
            self._warned.add(cause)
            logger.warning(message, *args)

    def _sending_unit(self, sample: Any, spec: TopicSpec) -> str | None:
        """
        Which peer sent `sample`, or None to drop it.

        A DataReader serves every publisher of its topic, so the sample is the
        only thing that can say. Senders the Interface does not list as
        publishing this topic are a third party's fault, not ours: warned about
        once, and dropped.
        """
        source = self._header_value(sample, self._source_field)
        code = None if source is None else int(source)
        if code is not None and code == self._own_unit_code:
            # Our own write, heard back by our own reader. Preferred to
            # `ignore_participant`, which would also block a legitimate second
            # process of ours on the same host.
            return None
        if code == 0 and self.config.unit_for_code(0) is None:
            code = None  # a header the sender never stamped
        if code is None:
            if len(spec.publishers) == 1:
                return spec.publishers[0]
            self._warn_once(
                ("no-header", spec.name),
                "topic %r: samples carry no stamped %r and the DDS Interface lists %d publishers "
                "of it %s, so the sender cannot be identified -- dropping",
                spec.name, self._source_path, len(spec.publishers), list(spec.publishers))
            return None
        unit_name = self.config.unit_for_code(code)
        if unit_name is None:
            self._warn_once(
                ("unknown-unit", spec.name, code),
                "topic %r: sample from unit code %#04x, which is not a peer of %s %s -- dropping",
                spec.name, code, self._dds.unit, self.config.unit_codes)
            return None
        if unit_name not in spec.publishers:
            self._warn_once(
                ("not-a-publisher", spec.name, unit_name),
                "topic %r: %s sent it, but the DDS Interface does not list %s as publishing it "
                "(publishers: %s) -- dropping",
                spec.name, unit_name, unit_name, list(spec.publishers))
            return None
        return unit_name

    # ------------------------------------------------------------------ #
    # Routing: topics, not opcodes
    # ------------------------------------------------------------------ #
    def _message_key(self, topic_id: OpCode) -> MessageKey:
        """A topic class, a sample of it, or its name -> the topic name."""
        return self._dds.topic_for(topic_id).name

    def _send_key(self, data: Any, opCode: OpCode | None) -> MessageKey:
        """
        A sample names its own topic, so `opCode` is optional on a send; given,
        it must name the same topic. With no sample (`stop_periodic`) the
        selector alone names it.
        """
        if data is None:
            return self._message_key(opCode)
        if isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(
                f"DDS publishes typed samples, not bytes: build an instance of one of "
                f"{self._dds.topic_names}")
        spec = self._dds.topic_for(data)
        if opCode is not None and self._message_key(opCode) != spec.name:
            raise ValueError(
                f"the sample is topic {spec.name!r}, but the call names topic {self._message_key(opCode)!r}")
        if not spec.publishes:
            raise ValueError(
                f"{self._dds.unit} does not publish {spec.name!r} in the DDS Interface (it only "
                f"subscribes it)")
        return spec.name

    def _send_unit(self, unit_name: str | None, key: MessageKey) -> UnitName | None:
        """None -- every subscriber of the topic -- unless the caller names one,
        which must then subscribe it. A named destination is stamped into the
        header; DDS still delivers to every subscriber."""
        if unit_name is None:
            return None
        spec = self._dds.topic_for(key)
        if unit_name not in spec.subscribers:
            raise ValueError(
                f"{unit_name!r} does not subscribe {spec.name!r} in the DDS Interface; its "
                f"subscribers are {list(spec.subscribers)}")
        return unit_name

    @staticmethod
    def _speakers(spec: TopicSpec) -> tuple[str, ...]:
        """The peers at the other end of `spec` from this unit."""
        if spec.direction is TopicDirection.SUBSCRIBE:
            return spec.publishers
        if spec.direction is TopicDirection.PUBLISH:
            return spec.subscribers
        return tuple(dict.fromkeys((*spec.publishers, *spec.subscribers)))

    def _resolve_route(self, unit_name: str | None, key: MessageKey) -> tuple[UnitName, RouteKey]:
        """
        `unit_name` may be left out whenever the Interface leaves no choice:
        when exactly one peer is at the other end of the topic. A unit usually
        has several peers, so the base rule -- "only when the connection has a
        single unit" -- would demand a name on nearly every call.
        """
        spec = self._dds.topic_named(key) if unit_name is None and isinstance(key, str) else None
        if spec is not None:
            speakers = self._speakers(spec)
            if not speakers:
                raise ValueError(f"topic {spec.name!r} has no peer at the other end in the DDS Interface")
            if len(speakers) > 1:
                raise ValueError(
                    f"unit_name is required: topic {spec.name!r} has {list(speakers)} at the other end")
            unit_name = speakers[0]
        return super()._resolve_route(unit_name, key)

    def _validate_route(self, unit_name: UnitName, route_key: RouteKey) -> None:
        """
        DDS registers no IRS layouts, so the base `validate_irs` could never
        pass here. The question it asks is still the right one -- "could this
        route ever deliver?" -- so it is asked of the Interface: a topic this
        unit does not subscribe, or a unit that does not publish it, is a
        subscription that would block forever.
        """
        _unit_code, key = route_key
        spec = self._dds.topic_for(key)
        if not spec.subscribes:
            raise ValueError(
                f"{self._dds.unit} does not subscribe {spec.name!r} in the DDS Interface (it only "
                f"publishes it), so nothing could ever arrive on this route")
        if unit_name not in spec.publishers:
            raise ValueError(
                f"{unit_name!r} does not publish {spec.name!r} in the DDS Interface; its "
                f"publishers are {list(spec.publishers)}")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def _do_start(self) -> None:
        global _live_connections
        # The participant is the unit of discovery and owns everything below
        # it; its QoS (transports, discovery peers) can only be set here. A
        # failure part-way is undone by `Connection._abort_start` -> `_do_stop`.
        self._participant = dds.DomainParticipant(self._dds.domain_id, self._participant_qos())
        for spec in self._dds.topics:
            self._create_entities(self._participant, spec)
        _live_connections += 1
        self._counted_live = True
        logger.info(
            "DDS unit %s joined domain %d: %d writer(s) %s, %d reader(s) %s",
            self._dds.unit, self._dds.domain_id, len(self._writers), list(self._writers),
            len(self._readers), list(self._readers))
        for unit in self.config.unit_names:
            self._mark_unit_connected(unit)

    def _create_entities(self, participant: dds.DomainParticipant, spec: TopicSpec) -> None:
        """
        One topic's entities. Creating the Topic is what registers the type with
        the participant: remote units match on (topic name, type name, QoS), so
        this is where a Python class becomes half of a wire contract.
        """
        topic = dds.Topic(participant, spec.name, spec.sample_type, qos=self._qos_for("topic", spec.name))
        if spec.publishes:
            self._writers[spec.name] = dds.DataWriter(
                participant.implicit_publisher, topic, self._qos_for("datawriter", spec.name))
            logger.info("DDS unit %s: DataWriter on %r (type %s)", self._dds.unit, spec.name, spec.type_name)
        if spec.subscribes:
            reader = dds.DataReader(
                participant.implicit_subscriber, topic, self._qos_for("datareader", spec.name))
            self._readers[spec.name] = reader
            self._read_tasks.append(self._track(self._read_loop(spec, reader)))
            logger.info("DDS unit %s: DataReader on %r (type %s)", self._dds.unit, spec.name, spec.type_name)

    async def _read_loop(self, spec: TopicSpec, reader: Any) -> None:
        """
        Feed every inbound sample on `spec` into the framework's single dispatch
        point, keyed by the topic and tagged with the unit that sent it.

        `take_data_async` exists only because this module imports `rti.asyncio`;
        its waitset dispatcher is created on first use, which is here -- on the
        shared loop thread, where it must be. `take_data` yields valid samples
        only, so the `valid_data` check callers would otherwise need is done.
        """
        try:
            async for sample in reader.take_data_async():
                try:
                    unit_name = self._sending_unit(sample, spec)
                    if unit_name is not None:
                        self._dispatch_incoming(unit_name, spec.name, sample)
                except Exception:
                    # One bad sample must not stop the topic.
                    logger.exception("DDS topic %s: dropping a sample that could not be routed", spec.name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("DDS read loop failed for topic %s", spec.name)

    async def _do_send(self, unit_name: UnitName | None, sample: Any, opcode: MessageKey) -> None:
        """Publish one sample on topic `opcode`, to every subscriber."""
        writer = self._writers.get(opcode)
        if writer is None:
            raise ConnectionError(f"topic {opcode!r} has no DataWriter yet: start() the connection first")
        self._stamp_outgoing(sample, unit_name)
        writer.write(sample)

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """
        Nothing to close for one unit.

        A DDS reader/writer belongs to a TOPIC and serves every unit speaking
        it, so closing entities here would cut off the other units too. Echo is
        refused on DDS configs, so nothing triggers this in practice.
        """
        logger.debug(
            "DDS unit %s marked disconnected; topic entities are shared and stay open", unit_name)

    async def _close_entities(self) -> None:
        """
        Stop the read loops, THEN close the participant.

        That order is the one piece of teardown RTI cannot do for us. Each read
        loop has a ReadCondition attached to `rti.asyncio`'s process-wide
        WaitSet, and cancelling the loop is what detaches it; closing the reader
        first would leave that shared WaitSet holding a condition of a closed
        entity. After that, a single `participant.close()` closes every topic,
        writer and reader the participant contains.
        """
        tasks, self._read_tasks = self._read_tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        participant, self._participant = self._participant, None
        if participant is not None:
            participant.close()
        self._writers.clear()
        self._readers.clear()

    async def _do_stop(self) -> None:
        global _live_connections
        writers, readers = len(self._writers), len(self._readers)
        await self._close_entities()
        logger.info(
            "DDS unit %s left domain %d (%d writer(s), %d reader(s) closed)",
            self._dds.unit, self._dds.domain_id, writers, readers)
        if self._counted_live:
            self._counted_live = False
            _live_connections = max(0, _live_connections - 1)
            if _live_connections == 0:
                # Shared with every other DdsConnection in this process, so only
                # the last one may tear it down.
                await rti_asyncio.close()
