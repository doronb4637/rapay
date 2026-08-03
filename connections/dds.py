"""
RTI Connext DDS connection.

DDS is data-centric and topic-based rather than socket/port based, but it
still honors the same JSON config / unit-routing contract as everything
else: each configured "port" is treated as a DDS domain id, and connected
units correspond to DDS Topics. Payloads are handed to/from Connext's Python
API natively -- NO (UnitCode,OpCode,DataLength) header is added or expected.

Requires the `rti.connextdds` package (RTI Connext Python API, Connext 6.1+).
If it isn't installed, importing this module raises ImportError, and
connection_framework/__init__.py simply skips registering the "dds" protocol
-- the rest of the system works fine without RTI present.

------------------------------------------------------------------------
Where IDL and QoS files fit
------------------------------------------------------------------------
DDS splits "what the data looks like" from "how the middleware behaves"
about it, and each half comes from its own file:

  * The IDL file answers WHAT. It declares the structs published on each
    topic (`struct TrackReport { long id; double lat; ... };`). DDS is
    strongly typed: a Topic cannot exist until its type is *registered*
    with the DomainParticipant, and publisher and subscriber must agree on
    that type or discovery refuses to match them.

  * The QoS (XML) file answers HOW. It declares named, reusable profiles
    (`<qos_profile name="Reliable">`) setting reliability, durability,
    history depth, deadline, liveliness, transport settings, and so on.
    QoS is applied per entity -- participant, topic, writer, reader -- and
    the writer's and reader's QoS must be *compatible* (RxO) or, again,
    they never match and no data flows.

Both are configured here as plain paths in the JSON config:

    {
      "protocol": "dds",
      "side": "publisher",
      "ip": "0.0.0.0",
      "ports": [0],                          # DDS domain id
      "units": {"0": "TrackUnit"},
      "idl_file": "types/tracks.idl",        # or a pre-converted .xml
      "qos_file": "qos/USER_QOS_PROFILES.xml",
      "qos_profile": "MyLib::Reliable",      # optional; else the XML default
      "topics": {"TrackUnit": "TrackTopic"}, # optional; defaults to unit name
      "types":  {"TrackUnit": "TrackReport"} # type name per unit
    }

The exact Connext calls, and the order they must happen in, are documented
inline in `_do_start()` below.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .base import Connection
from .config import ConnectionConfig

logger = logging.getLogger("connmgr.dds")

import rti.connextdds as dds  # type: ignore  # raises ImportError if not installed

# DDS has no (UnitCode,OpCode,DataLength) header of its own, so there is no
# natural opcode to parse off an inbound sample. Every inbound DDS sample
# is dispatched under this fixed opcode; callers subscribe to DDS traffic
# with `receive_message(opcode=DDS_DEFAULT_OPCODE, ...)`. Deployments that
# need real opcode-style routing over DDS should encode it as a field of
# their DDS type and branch on it themselves after receiving.
DDS_DEFAULT_OPCODE = 0


class DdsConnection(Connection):
    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._participant: dds.DomainParticipant | None = None
        self._writers: dict[str, dds.DynamicData.DataWriter] = {}
        self._readers: dict[str, dds.DynamicData.DataReader] = {}

        # QosProvider built from `qos_file`: the object that parses the QoS
        # XML and hands out QoS policy objects by profile name.
        self._qos_provider: dds.QosProvider | None = None
        # QosProvider built from `idl_file`: in Connext, the *same* class
        # also serves as the type provider, because the XML it reads may
        # contain a <types> section as well as <qos_library> sections.
        self._type_provider: dds.QosProvider | None = None

        self._idl_file: str | None = config.extra.get("idl_file")
        self._qos_file: str | None = config.extra.get("qos_file")
        self._qos_profile: str | None = config.extra.get("qos_profile")
        self._topics_cfg: dict[str, str] = config.extra.get("topics") or {}
        self._types_cfg: dict[str, str] = config.extra.get("types") or {}
        self._temp_dirs: list[tempfile.TemporaryDirectory[str]] = []

    # ------------------------------------------------------------------ #
    # IDL -> type registration
    # ------------------------------------------------------------------ #
    async def _load_type_provider(self) -> dds.QosProvider | None:
        """
        Turn `idl_file` into something Connext can register as a DDS type.

        Connext's Python API cannot parse raw .idl at runtime -- IDL is an
        input to the code generator, not to the middleware. There are two
        supported routes from IDL to a usable type, and this method covers
        the runtime one:

          1. AHEAD OF TIME (`rtiddsgen -language python tracks.idl`) emits a
             Python module of @idl.struct dataclasses. You then import it and
             pass the class straight to `dds.Topic(participant, name, Track)`.
             Fastest and type-checked, but the generated module has to be on
             PYTHONPATH -- so it is out of reach of a purely JSON-driven
             factory like this one.

          2. AT RUNTIME (what we do here) via the XML type representation.
             `rtiddsgen -convertToXml tracks.idl` rewrites the IDL structs
             as `<types>` XML. `dds.QosProvider(<that xml>)` parses it, and
             `provider.type("TrackReport")` returns a `dds.DynamicType`
             describing the struct. A DynamicType can be handed to
             `dds.DynamicData.Topic(...)`, which is what registers the type
             with the DomainParticipant under its name -- the step that must
             happen before any Topic of that type can be created.

        So: `.xml` is loaded directly; `.idl` is converted first (once, into
        a temp dir, via rtiddsgen). The conversion shells out, so it runs in
        an executor -- never block the shared event loop thread.
        """
        if not self._idl_file:
            return None

        idl_path = Path(self._idl_file)
        if not idl_path.is_file():
            raise FileNotFoundError(f"config['idl_file'] not found: {idl_path}")

        if idl_path.suffix.lower() == ".xml":
            xml_path = idl_path  # already in the XML type representation
        else:
            loop = asyncio.get_running_loop()
            xml_path = await loop.run_in_executor(None, self._convert_idl_to_xml, idl_path)

        # Parsing the XML happens here; the types inside it are only actually
        # *registered* with a participant when a Topic is created from one
        # (see _do_start), because registration is per-participant.
        return dds.QosProvider(str(xml_path))

    def _convert_idl_to_xml(self, idl_path: Path) -> Path:
        """Run `rtiddsgen -convertToXml` (blocking; called in an executor)."""
        rtiddsgen = shutil.which("rtiddsgen") or shutil.which("rtiddsgen.bat")
        if rtiddsgen is None:
            raise RuntimeError(
                f"cannot load {idl_path.name}: rtiddsgen is not on PATH. Either install "
                f"RTI Connext (NDDSHOME/bin), or pre-convert once with "
                f"`rtiddsgen -convertToXml {idl_path}` and point config['idl_file'] at "
                f"the resulting .xml"
            )
        # Held on the instance so the generated XML outlives this call but is
        # still cleaned up in _do_stop().
        temp_dir = tempfile.TemporaryDirectory(prefix="connmgr-idl-")
        self._temp_dirs.append(temp_dir)
        subprocess.run(
            [rtiddsgen, "-convertToXml", "-d", temp_dir.name, str(idl_path)],
            check=True,
            capture_output=True,
        )
        xml_path = Path(temp_dir.name) / f"{idl_path.stem}.xml"
        if not xml_path.is_file():
            raise RuntimeError(f"rtiddsgen produced no XML for {idl_path}")
        logger.info("converted %s -> %s", idl_path, xml_path)
        return xml_path

    def _resolve_type(self, unit: str) -> Any:
        """
        The DDS type for `unit`'s topic.

        Preference order:
          1. `config.extra['type_resolver'](unit)` -- an escape hatch for
             code-generated types (route 1 above), where the caller passes a
             live Python class this factory could never have imported itself.
          2. The type provider loaded from `idl_file`, looked up by the type
             name in `config['types'][unit]` (defaulting to the unit name).
             `QosProvider.type(name)` is the call that pulls a DynamicType
             out of the parsed XML.
        """
        type_resolver = self.config.extra.get("type_resolver")
        if type_resolver is not None:
            return type_resolver(unit)

        if self._type_provider is None:
            raise ValueError(
                f"no DDS type for unit {unit!r}: supply config['idl_file'] (an .idl or "
                f"converted .xml) or config.extra['type_resolver']"
            )
        type_name = self._types_cfg.get(unit, unit)
        try:
            return self._type_provider.type(type_name)
        except Exception as exc:  # noqa: BLE001 - surfaced with actionable context
            raise ValueError(
                f"type {type_name!r} not found in {self._idl_file!r} for unit {unit!r}; "
                f"set config['types'][{unit!r}] to the correct IDL struct name"
            ) from exc

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
            raise FileNotFoundError(f"config['qos_file'] not found: {qos_path}")
        logger.info("loading DDS QoS profiles from %s", qos_path)
        return dds.QosProvider(str(qos_path))

    def _qos_for(self, entity: str) -> Any:
        """
        Fetch the QoS policy object for one entity kind.

        Each entity kind has its own accessor on QosProvider, because each
        has its own policy set:

            participant_qos_from_profile(profile) -> DomainParticipantQos
            topic_qos_from_profile(profile)       -> TopicQos
            datawriter_qos_from_profile(profile)  -> DataWriterQos
            datareader_qos_from_profile(profile)  -> DataReaderQos

        With no `qos_profile` configured we use the provider's default
        profile (the `participant_qos` / `datawriter_qos` / ... properties),
        which is the one marked `is_default_qos="true"` in the XML. With no
        QoS file at all we return None and let each constructor fall back to
        the DDS spec defaults (best-effort, volatile, ...).

        The returned object is applied at CONSTRUCTION time, below -- QoS is
        largely immutable once an entity exists, which is why every entity is
        built with its QoS in hand rather than configured afterwards.
        """
        if self._qos_provider is None:
            return None
        if self._qos_profile:
            getter = getattr(self._qos_provider, f"{entity}_qos_from_profile")
            return getter(self._qos_profile)
        return getattr(self._qos_provider, f"{entity}_qos")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def _do_start(self) -> None:
        # -- 1. Parse both files up front. Neither touches the network yet;
        #       this is pure XML parsing, so a typo fails fast and loudly
        #       before any DDS entity exists.
        self._qos_provider = self._load_qos_provider()
        self._type_provider = await self._load_type_provider()

        # -- 2. The DomainParticipant is the root entity and the unit of
        #       discovery: it joins the domain, starts the discovery
        #       endpoints, and owns every topic/reader/writer created under
        #       it. Its QoS (transports, discovery peers, buffer sizes) can
        #       only be set here, at construction.
        #       Convention: the sole configured "port" doubles as the domain id.
        domain_id = self.config.ports[0]
        participant_qos = self._qos_for("participant")
        self._participant = (
            dds.DomainParticipant(domain_id, participant_qos)
            if participant_qos is not None
            else dds.DomainParticipant(domain_id)
        )

        is_publisher = self.config.side.value in ("server", "publisher", "sender")
        is_subscriber = (
            self.config.side.value in ("client", "subscriber", "receiver")
            or self.config.extra.get("duplex")
        )

        for unit in self.config.connected_units:
            topic_name = self._topics_cfg.get(unit, unit)

            # -- 3. Creating the Topic is what REGISTERS the type with the
            #       participant: the DynamicType from the IDL/XML is bound to
            #       `topic_name` in this domain. Remote applications match on
            #       (topic name, type name, QoS compatibility), so this is the
            #       point where the IDL file stops being a file and starts
            #       being part of the wire contract.
            topic_qos = self._qos_for("topic")
            topic = (
                dds.DynamicData.Topic(
                    self._participant, topic_name, self._resolve_type(unit), topic_qos
                )
                if topic_qos is not None
                else dds.DynamicData.Topic(self._participant, topic_name, self._resolve_type(unit))
            )

            # -- 4. Writers and readers carry the QoS that actually governs
            #       delivery (reliability, durability, history, deadline).
            #       These are the policies checked for RxO compatibility
            #       during discovery: mismatch them and the pair simply never
            #       connects -- no error, just silence -- which is why they
            #       come from one shared profile in the QoS file.
            if is_publisher:
                writer_qos = self._qos_for("datawriter")
                self._writers[unit] = (
                    dds.DynamicData.DataWriter(
                        self._participant.implicit_publisher, topic, writer_qos
                    )
                    if writer_qos is not None
                    else dds.DynamicData.DataWriter(self._participant.implicit_publisher, topic)
                )
            if is_subscriber:
                reader_qos = self._qos_for("datareader")
                reader = (
                    dds.DynamicData.DataReader(
                        self._participant.implicit_subscriber, topic, reader_qos
                    )
                    if reader_qos is not None
                    else dds.DynamicData.DataReader(self._participant.implicit_subscriber, topic)
                )
                self._readers[unit] = reader
                self._track(self._read_loop(unit, reader))

    async def _read_loop(self, unit: str, reader: dds.DynamicData.DataReader) -> None:
        # Recent Connext Python releases expose an async iterator over
        # incoming samples; adapt this to whichever Connext version you run
        # (older releases may need a StatusCondition + WaitSet bridged onto
        # the loop via run_in_executor instead).
        try:
            async for sample in reader.take_data_async():
                # Unlike the framed protocols, the payload here is a native
                # DDS sample object, not bytes -- see the module docstring.
                self._dispatch_incoming(unit, DDS_DEFAULT_OPCODE, sample)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("DDS read loop failed for unit %s", unit)

    async def _do_send(self, unit_name: str, data: bytes, opcode: int) -> None:
        # opcode is accepted for interface consistency with every other
        # Connection subclass, but DDS carries payloads natively -- there is
        # no header to stamp it into (see DDS_DEFAULT_OPCODE above).
        writer = self._writers.get(unit_name)
        if writer is None:
            raise ConnectionError(f"No DDS writer for unit {unit_name!r}")
        writer.write(data)

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """Close just this unit's reader/writer after an echo timeout. The
        DomainParticipant stays alive so the remaining units keep running."""
        reader = self._readers.pop(unit_name, None)
        writer = self._writers.pop(unit_name, None)
        if reader is None and writer is None:
            return
        logger.warning("DDS unit %s: closing reader/writer after echo timeout", unit_name)
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()

    async def _do_stop(self) -> None:
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
        self._type_provider = None
        for temp_dir in self._temp_dirs:
            temp_dir.cleanup()
        self._temp_dirs.clear()
