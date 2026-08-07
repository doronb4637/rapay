"""
RTI Connext DDS connection.

DDS is data-centric and topic-based rather than socket/port based, but it
still honors the same JSON config / unit-routing contract as everything
else: each connection entry's "port" is treated as a DDS domain id, and each
connection name corresponds to a DDS Topic. Payloads are handed to/from
Connext's Python API natively -- NO (UnitCode,OpCode,DataLength) header is
added or expected, and `_do_send` takes a typed sample instance rather than
bytes.

Requires the `rti.connextdds` package (RTI Connext Python API, Connext 6.1+).
If it isn't installed, importing this module raises ImportError, and
connections/__init__.py simply skips registering the "dds" protocol -- the
rest of the system works fine without RTI present.

------------------------------------------------------------------------
Where the IDL and QoS files fit
------------------------------------------------------------------------
DDS splits "what the data looks like" from "how the middleware behaves"
about it, and each half comes from its own file:

  * The IDL file answers WHAT. Here it is a *Python* module using the
    `rti.types` decorators rather than a text `.idl`:

        import rti.types as idl

        @idl.struct
        class Point:
            x: int = 0
            y: int = 0

    `@idl.struct` turns an ordinary annotated class into a DDS type: it
    builds the TypeSupport that Connext uses to serialize instances and to
    publish the type's definition during discovery. Because it is already
    Python, there is no code-generation step at all -- the module is
    imported and the class handed straight to `dds.Topic(...)`. DDS is
    strongly typed: a Topic cannot exist until its type is registered with
    the DomainParticipant, and publisher and subscriber must agree on that
    type or discovery refuses to match them.

  * The QoS (XML) file answers HOW. It declares named, reusable profiles
    (`<qos_profile name="Reliable">`) setting reliability, durability,
    history depth, deadline, liveliness, transport settings, and so on.
    QoS is applied per entity -- participant, topic, writer, reader -- and
    the writer's and reader's QoS must be *compatible* (RxO) or, again,
    they never match and no data flows.

Both are configured as plain paths in the JSON config:

    {
      "protocol": "dds",
      "side": "publisher",
      "ip": "0.0.0.0",
      "connections": {
        "TrackUnit": {"port": 0, "unitCode": 1}   # port == DDS domain id
      },
      "idl_file": "types/tracks.py",         # Python module of @idl.struct types
      "qos_file": "qos/USER_QOS_PROFILES.xml",
      "qos_profile": "MyLib::Reliable",      # optional; else the XML default
      "topics": {"TrackUnit": "TrackTopic"}, # optional; defaults to unit name
      "types":  {"TrackUnit": "Point"}       # class name per unit
    }

The exact Connext calls, and the order they must happen in, are documented
inline in `_do_start()` below.

Note that the echo lifecycle in `base.Connection` sends `bytes`, so it does
not apply to DDS: leave the echo opcodes unset on DDS configs and use DDS's
own liveliness QoS, which exists for exactly this purpose.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .base import Connection
from .config import ConnectionConfig

logger = logging.getLogger("connmgr.dds")

import rti.connextdds as dds  # type: ignore  # raises ImportError if not installed
import rti.types as idl  # type: ignore

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
        self._writers: dict[str, Any] = {}
        self._readers: dict[str, Any] = {}

        # QosProvider built from `qos_file`: the object that parses the QoS
        # XML and hands out QoS policy objects by profile name.
        self._qos_provider: dds.QosProvider | None = None
        # The imported Python IDL module -- the namespace every @idl.struct
        # type for this connection is looked up in.
        self._idl_module: ModuleType | None = None

        self._idl_file: str | None = config.extra.get("idl_file")
        self._qos_file: str | None = config.extra.get("qos_file")
        self._qos_profile: str | None = config.extra.get("qos_profile")
        self._topics_cfg: dict[str, str] = config.extra.get("topics") or {}
        self._types_cfg: dict[str, str] = config.extra.get("types") or {}

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
                f"split them into separate connections if they really are on different domains"
            )
        return ports[0]

    # ------------------------------------------------------------------ #
    # Python IDL -> DDS types
    # ------------------------------------------------------------------ #
    async def _load_idl_module(self) -> ModuleType | None:
        """
        Import the Python IDL module named by `idl_file`.

        The file is a normal Python module that happens to define
        `@idl.struct` classes, so "loading the IDL" is just an import -- no
        `rtiddsgen`, no XML, no generated code to keep on PYTHONPATH. It is
        loaded by path via importlib rather than by module name so a config
        can point anywhere on disk.

        Importing executes the module, which is why it runs in an executor:
        module-level work (and whatever it imports in turn) must never block
        the shared event-loop thread. The module is registered in
        `sys.modules` before execution, both so that dataclass/typing
        machinery inside `@idl.struct` can resolve its own module by name and
        so a second connection pointing at the same file reuses the already
        imported types -- DDS type identity is per-class, so re-importing
        would produce two distinct types with the same name.
        """
        if not self._idl_file:
            return None

        idl_path = Path(self._idl_file).resolve()
        if not idl_path.is_file():
            raise FileNotFoundError(f"config['idl_file'] not found: {idl_path}")
        if idl_path.suffix != ".py":
            raise ValueError(
                f"config['idl_file'] must be a Python module defining @idl.struct types, "
                f"got {idl_path.name}"
            )

        module_name = f"_connmgr_idl_{idl_path.stem}"
        cached = sys.modules.get(module_name)
        if cached is not None:
            return cached

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._import_module, module_name, idl_path)

    @staticmethod
    def _import_module(module_name: str, idl_path: Path) -> ModuleType:
        """Import `idl_path` under `module_name` (blocking; runs in an executor)."""
        spec = importlib.util.spec_from_file_location(module_name, idl_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load a Python module from {idl_path}")
        module = importlib.util.module_from_spec(spec)
        # Registered before exec_module so the module can be found by name
        # while its own body is still running.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)  # don't cache a half-built module
            raise
        logger.info("loaded DDS IDL module %s from %s", module_name, idl_path)
        return module

    def _resolve_type(self, unit: str) -> Any:
        """
        The DDS type class for `unit`'s topic.

        Preference order:
          1. `config.extra['type_resolver'](unit)` -- an escape hatch for
             callers holding a live type class this JSON-driven factory could
             never have imported itself.
          2. `getattr(idl_module, config['types'][unit])`, defaulting to the
             unit's own name as the class name.

        The result is checked to be an actual DDS type: `@idl.struct` attaches
        TypeSupport to the class, and `idl.get_type_support()` is what fails
        if the decorator was forgotten. Catching that here turns a confusing
        error deep inside Connext's Topic constructor into one that names the
        class and the file it came from.
        """
        type_resolver = self.config.extra.get("type_resolver")
        if type_resolver is not None:
            return type_resolver(unit)

        if self._idl_module is None:
            raise ValueError(
                f"no DDS type for unit {unit!r}: supply config['idl_file'] (a Python module "
                f"of @idl.struct classes) or config.extra['type_resolver']"
            )

        type_name = self._types_cfg.get(unit, unit)
        type_cls = getattr(self._idl_module, type_name, None)
        if type_cls is None:
            available = [n for n in vars(self._idl_module) if not n.startswith("_")]
            raise ValueError(
                f"type {type_name!r} not found in {self._idl_file!r} for unit {unit!r}; "
                f"set config['types'][{unit!r}] to one of: {available}"
            )
        try:
            idl.get_type_support(type_cls)
        except Exception as exc:  # noqa: BLE001 - surfaced with actionable context
            raise TypeError(
                f"{type_name!r} in {self._idl_file!r} is not a DDS type; decorate it with "
                f"@idl.struct (from `import rti.types as idl`)"
            ) from exc
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
        # -- 1. Load both files up front. Neither touches the network; this
        #       is XML parsing and a Python import, so a typo fails fast and
        #       loudly before any DDS entity exists.
        self._qos_provider = self._load_qos_provider()
        self._idl_module = await self._load_idl_module()

        # -- 2. The DomainParticipant is the root entity and the unit of
        #       discovery: it joins the domain, starts the discovery
        #       endpoints, and owns every topic/reader/writer created under
        #       it. Its QoS (transports, discovery peers, buffer sizes) can
        #       only be set here, at construction.
        participant_qos = self._qos_for("participant")
        self._participant = (
            dds.DomainParticipant(self.domain_id, participant_qos)
            if participant_qos is not None
            else dds.DomainParticipant(self.domain_id)
        )

        is_publisher = self.config.side.value in ("server", "publisher", "sender")
        is_subscriber = (
            self.config.side.value in ("client", "subscriber", "receiver")
            or self.config.extra.get("duplex")
        )

        for unit in self.config.connected_units:
            topic_name = self._topics_cfg.get(unit, unit)
            type_cls = self._resolve_type(unit)

            # -- 3. Creating the Topic is what REGISTERS the type with the
            #       participant: the @idl.struct class's TypeSupport is bound
            #       to `topic_name` in this domain. Remote applications match
            #       on (topic name, type name, QoS compatibility), so this is
            #       the point where the IDL module stops being a file and
            #       starts being part of the wire contract.
            topic_qos = self._qos_for("topic")
            topic = (
                dds.Topic(self._participant, topic_name, type_cls, topic_qos)
                if topic_qos is not None
                else dds.Topic(self._participant, topic_name, type_cls)
            )

            # -- 4. Writers and readers carry the QoS that actually governs
            #       delivery (reliability, durability, history, deadline).
            #       These are the policies checked for RxO compatibility
            #       during discovery: mismatch them and the pair simply never
            #       connects -- no error, just silence -- which is why they
            #       come from one shared profile in the QoS file.
            #       dds.DataWriter/DataReader are generic over the type and
            #       infer it from the topic they are built on.
            if is_publisher:
                writer_qos = self._qos_for("datawriter")
                self._writers[unit] = (
                    dds.DataWriter(self._participant.implicit_publisher, topic, writer_qos)
                    if writer_qos is not None
                    else dds.DataWriter(self._participant.implicit_publisher, topic)
                )
            if is_subscriber:
                reader_qos = self._qos_for("datareader")
                reader = (
                    dds.DataReader(self._participant.implicit_subscriber, topic, reader_qos)
                    if reader_qos is not None
                    else dds.DataReader(self._participant.implicit_subscriber, topic)
                )
                self._readers[unit] = reader
                self._track(self._read_loop(unit, reader))

            # Discovery is asynchronous and peer-driven, so there is no
            # handshake to wait on: a unit is usable here once its own
            # reader/writer exist.
            self._mark_unit_connected(unit)

    async def _read_loop(self, unit: str, reader: Any) -> None:
        """Feed every inbound sample for `unit` into the framework's single
        dispatch point. Recent Connext Python releases expose an async
        iterator over incoming samples; adapt this to whichever Connext
        version you run (older releases may need a StatusCondition + WaitSet
        bridged onto the loop via run_in_executor instead)."""
        try:
            async for sample in reader.take_data_async():
                # Unlike the framed protocols, the payload here is a typed
                # DDS sample instance, not bytes -- see the module docstring.
                self._dispatch_incoming(unit, DDS_DEFAULT_OPCODE, sample)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("DDS read loop failed for unit %s", unit)

    async def _do_send(self, unit_name: str, data: bytes, opcode: int) -> None:
        """
        Publish one sample on `unit_name`'s topic.

        `data` must be an instance of that unit's `@idl.struct` type, not
        bytes: DDS serializes through the type's TypeSupport, so there is
        nothing for this layer to frame. `opcode` is accepted for interface
        consistency with every other Connection subclass but has no header to
        live in (see DDS_DEFAULT_OPCODE).
        """
        writer = self._writers.get(unit_name)
        if writer is None:
            raise ConnectionError(f"No DDS writer for unit {unit_name!r}")
        writer.write(data)

    async def _do_disconnect_unit(self, unit_name: str) -> None:
        """Close just this unit's reader/writer. The DomainParticipant stays
        alive so the remaining units keep running."""
        reader = self._readers.pop(unit_name, None)
        writer = self._writers.pop(unit_name, None)
        if reader is None and writer is None:
            return
        logger.warning("DDS unit %s: closing reader/writer", unit_name)
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()
        self._mark_unit_disconnected(unit_name)

    async def _do_stop(self) -> None:
        """Close every DDS entity, innermost first: readers and writers
        before the participant that owns them."""
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
        # The imported IDL module is deliberately left in sys.modules: its
        # type classes carry DDS type identity, and re-importing on a later
        # start() would mint distinct types with the same name.
