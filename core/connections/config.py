"""
JSON-driven configuration objects for connections.

Example JSON (a TCP server multiplexing two units over two ports):

{
  "protocol": "tcp",
  "side": "server",
  "ip": "127.0.0.1",
  "local_ip": "127.0.0.1",
  "unitCode": 1,
  "connections": {
    "RadarUnit":   {"port": 2000, "unitCode": 7, "echo_opcode": 10,
                    "Structures": ["Radar.radar_link"]},
    "TrackerUnit": {"port": 2001, "unitCode": 8,
                    "Structures": ["Tracker.tracker_link"]}
  },
  "echo_opcode": 99,
  "EchoInterval": 1.0,
  "EchoTimeout": 5.0
}

There are two kinds of unit code here, and the distinction is the whole point:

  * The top-level `"unitCode"` is OUR OWN code -- who this process is on the
    wire. It is REQUIRED, and it is the value stamped into every message this
    connection sends, so a peer can tell who sent it.
  * Each `"connections"[name]["unitCode"]` is THEIR code -- the remote unit's
    identity. It is REQUIRED for every connection (no default/derived value),
    and it keys the routing tables and is the code handed to the parser when
    decoding what that unit sent us.

`"connections"` is the single source of truth for unit routing: it maps each
connection name (the logical unit) to the port it lives on and the numeric
unit code that unit identifies itself with. It is REQUIRED -- there is no
implicit/"default" unit, and no separate port list to keep in sync with it.

Everything not in the fixed key set above lands in `extra` and is parsed by
whoever owns it: the echo keys by `EchoSettings` (below), protocol-specific
keys (ttl, mode, idl_file, qos_file, ...) by the individual protocol classes.

The echo keys are HIERARCHICAL: the same spellings are accepted at the
connection level (in `extra`, the shared default for every unit) and inside
an individual unit's dict (that unit's override). `EchoSettings.resolve()`
merges the two, so in the example above RadarUnit heartbeats on opcode 10
while TrackerUnit falls back to the connection-wide 99 -- both at the shared
1.0s/5.0s timings.

`Structures` is hierarchical in the same shape but with a stricter rule, because
a structures file defines the IRS for ONE link (see connections/CLAUDE.md 2b):
each unit names its own, and a connection-level list is only accepted when
there is exactly one unit to apply it to -- or on multicast, where one sender
genuinely does fan out to many receivers over a single shared IRS. Anything
else is a load-time ValueError, since applying one list to several links is
what let two files silently overwrite each other's layouts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from core.tools.general import (resolve_module_name, topic_opcode, validated_opcode,
                               validated_unitcode)
from core.annotations import *

DEFAULT_ECHO_INTERVAL: float = 1.0
DEFAULT_ECHO_TIMEOUT: float = 5.0

#: Every json key defined
PROTOCOL_KEY = "protocol"
SIDE_KEY = "side"
IP_KEY = "ip"
CONNECTIONS_KEY = "connections"
PORT_KEY = "port"
UNIT_CODE_KEYS = ("UnitCode", "unitCode", "unit_code")
LOCAL_IP_KEYS = ("local_ip", "localIp")
#: The IRS message layouts a link uses. Accepted at BOTH levels -- inside a
#: unit's dict in `connections` (that link's own), and at the connection level,
#: which is only legal when there is one link to be had (see `from_json`).
STRUCTURES_KEYS = ("Structures", "structures")

#: The symmetric "one opcode, both directions" spelling ONLY -- kept distinct
#: from ALL_ECHO_OPCODE_KEYS below. `from_extra` looks up `shared` through
#: this tuple specifically; using the union here would let a lone
#: recv_echo_opcode/send_echo_opcode get misread as the shared value and leak
#: into the direction that was never actually configured.
ECHO_OPCODE_KEYS = ("echo_opcode", "EchoOpcode", "echoOpcode")
RECV_ECHO_OPCODE_KEYS = ("recv_echo_opcode", "RecvEchoOpcode", "recvEchoOpcode")
SEND_ECHO_OPCODE_KEYS = ("send_echo_opcode", "SendEchoOpcode", "sendEchoOpcode")
#: All three opcode-key families combined -- for the places that mean "any
#: spelling of any opcode key", e.g. `resolve()`'s "the opcode keys resolve as
#: a group" check. NOT a substitute for ECHO_OPCODE_KEYS above.
ALL_ECHO_OPCODE_KEYS: tuple[str, ...] = (*ECHO_OPCODE_KEYS, *RECV_ECHO_OPCODE_KEYS, *SEND_ECHO_OPCODE_KEYS)

ECHO_INTERVAL_KEYS = ("echo_interval", "EchoInterval", "echoInterval")
ECHO_TIMEOUT_KEYS = ("echo_timeout", "EchoTimeout", "echoTimeout")
ECHO_PAYLOAD_KEYS = ("echo_payload", "EchoPayload", "echoPayload")
ECHO_TUNING_KEYS: tuple[str, ...] = (*ECHO_INTERVAL_KEYS, *ECHO_TIMEOUT_KEYS, *ECHO_PAYLOAD_KEYS)

ECHO_KEYS: frozenset[str] = frozenset(ALL_ECHO_OPCODE_KEYS + ECHO_TUNING_KEYS)

#: DDS topics. A list, not a dict keyed by unit: a topic and a unit are
#: independent axes there -- one unit speaks several topics, and one topic
#: carries traffic from several units -- so keying topics by unit (as this
#: config used to) could express neither.
TOPICS_KEY = "topics"
TOPIC_NAME_KEYS = ("topic", "Topic", "name")
TOPIC_TYPE_KEYS = ("type", "Type")
TOPIC_TYPE_NAME_KEYS = ("type_name", "typeName", "TypeName")
TOPIC_DIRECTION_KEYS = ("direction", "Direction")
TOPIC_OPCODE_KEYS = ("opcode", "opCode", "OpCode")


class Protocol(str, Enum):
    TCP = "tcp"
    UDP = "udp"
    MULTICAST = "multicast"
    DDS = "dds"


class Side(str, Enum):
    # TCP
    CLIENT = "client"
    SERVER = "server"
    # DDS
    PUBLISHER = "publisher"
    SUBSCRIBER = "subscriber"
    # UDP / MULTICAST
    SENDER = "sender"
    RECEIVER = "receiver"


class TopicDirection(str, Enum):
    """
    Which DDS entities a topic gets on this connection.

    Per TOPIC, not per connection: a real unit is normally duplex on one
    participant -- publishing some topics while subscribing others -- which a
    single connection-wide `side` cannot describe. `side` only supplies the
    default when a topic entry doesn't say.
    """
    PUBLISH = "publish"
    SUBSCRIBE = "subscribe"
    BOTH = "both"


# --------------------------------------------------------------------------- #
# Typed coercion helpers for raw JSON. Coercing once, here, lets the rest of
# the codebase assume real int/float/bytes values and never re-check.
# --------------------------------------------------------------------------- #
def _lookup(source: dict[str, Any], *names: str) -> Any | None:
    """Returns first present. Keys are accepted in both
    the snake_case, camelCase and PascalCase"""
    for name in names:
        value = source.get(name)
        if value is not None:
            return value
    return None


def _as_opcode(value: Any, field_name: str) -> UnitCode:
    """Checks opCode, extracted form config.
    uses 'tools.general.validated_opCode' for allowing both HEX and DEC integers
    also validate UInt16 size."""
    try:
        opcode = validated_opcode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config['{field_name}'] must be an integer opcode, got {value!r}") from exc
    if not 0 <= opcode <= 0xFFFF:
        raise ValueError(f"config['{field_name}'] = {opcode} does not fit the UInt16 OpCode header field")
    return opcode


def _as_unit_code(value: Any, field_name: str) -> int:
    """Checks unitCode, extracted form config.
    uses 'tools.general.validated_opCode' for allowing both HEX and DEC integers
    also validate UInt8 size."""
    try:
        code = validated_unitcode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config['{field_name}'] must be an integer unit code, got {value!r}") from exc
    if not 0 <= code <= 0xFF:
        raise ValueError(f"config['{field_name}'] = {code} does not fit the UInt8 UnitCode header field")
    return code


def _as_positive_float(value: Any, field_name: str, default: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config['{field_name}'] must be a number of seconds, got {value!r}") from exc
    if number <= 0:
        raise ValueError(f"config['{field_name}'] must be > 0, got {number}")
    return number


@dataclass(frozen=True, slots=True)
class EchoSettings:
    """
    *Immutable class*
    Everything the echo lifecycle needs, parsed and validated once.

    Two ways to configure the opcodes:

      * `"echo_opcode": 99`               -- one opcode used for BOTH directions
                                             (the ordinary symmetric heartbeat)
      * `"recv_echo_opcode": 99,`         -- distinct inbound/outbound opcodes,
        `"send_echo_opcode": 100`            for peers that heartbeat asymmetrically

    A shared `echo_opcode` may still be overridden in one direction by also
    supplying `recv_echo_opcode` or `send_echo_opcode`. Both directions must
    resolve for the feature to activate at all (see `enabled`).

    All of these keys are accepted at BOTH levels of the config -- inside
    `extra` (the connection-wide default) and inside an individual unit's dict
    in `connections`. `resolve()` is what merges the two into the settings one
    unit actually runs on.
    """

    recv_opcode: int | None = None
    send_opcode: int | None = None
    interval: float = DEFAULT_ECHO_INTERVAL
    timeout: float = DEFAULT_ECHO_TIMEOUT

    @property
    def enabled(self) -> bool:
        """Return whether the echo machinery should run."""
        return self.recv_opcode is not None and self.send_opcode is not None

    @classmethod
    def from_extra(cls, extra: dict[str, Any]) -> EchoSettings:
        """Parse the echo block out of a config's `extra` dict, raising
        `ValueError` on anything malformed so a typo is a load-time failure
        rather than a link that silently never heartbeats."""
        shared = _lookup(extra, *ECHO_OPCODE_KEYS)
        recv = _lookup(extra, *RECV_ECHO_OPCODE_KEYS)
        send = _lookup(extra, *SEND_ECHO_OPCODE_KEYS)
        if shared is not None:
            if recv is None:
                recv = shared
            if send is None:
                send = shared

        interval = _as_positive_float(_lookup(extra, *ECHO_INTERVAL_KEYS), "EchoInterval", DEFAULT_ECHO_INTERVAL)
        timeout = _as_positive_float(_lookup(extra, *ECHO_TIMEOUT_KEYS), "EchoTimeout", DEFAULT_ECHO_TIMEOUT)
        if timeout <= interval:
            raise ValueError(f"EchoTimeout ({timeout}s) must be greater than EchoInterval ({interval}s), "
                             f"otherwise the link is declared dead before the next echo is even due")

        return cls(
            recv_opcode=None if recv is None else _as_opcode(recv, "RecvEchoOpcode"),
            send_opcode=None if send is None else _as_opcode(send, "SendEchoOpcode"),
            interval=interval, timeout=timeout
        )

    @classmethod
    def resolve(cls, unit_spec: Mapping[str, Any], global_extra: Mapping[str, Any]) -> EchoSettings:
        """
        The echo settings ONE unit actually runs on: its own keys layered over
        the connection-wide block in `extra`.

        A connection multiplexing several units may well be talking to peers that heartbeat
        on different opcodes -- or to one peer that heartbeats and one that
        doesn't.

        Two levels of granularity, deliberately:

          * `EchoInterval` / `EchoTimeout` resolve
            **individually**, because each is independently meaningful: a unit
            overriding just its timeout still wants the shared interval, and
            the `timeout > interval` check runs on whatever the merge produced.

        To configure a unit to not have echo just define it's echo as 'null' value in the json config.
        """
        merged = {key: value for key, value in global_extra.items() if key in ECHO_KEYS}
        if any(unit_spec.get(key) is not None for key in ALL_ECHO_OPCODE_KEYS):
            for key in ALL_ECHO_OPCODE_KEYS:
                merged.pop(key, None)
        merged.update({k: v for k, v in unit_spec.items() if k in ECHO_KEYS})
        return cls.from_extra(merged)


def _structures_from(source: Mapping[str, Any], field_name: str) -> tuple[str, ...] | None:
    """The raw `Structures` list written at ONE config level, or None if absent.

    A bare string is accepted as a one-element list, since `import_modules`
    already takes that spelling. An explicit empty list is NOT None -- it means
    "this level says: none", and overrides a connection-level default.
    """
    value = _lookup(source, *STRUCTURES_KEYS)
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            f"config['{field_name}'] must be a list of IRS structures modules, got {value!r}")
    cleaned = tuple(str(entry).strip() for entry in value if str(entry).strip())
    return cleaned


def resolve_structures(unit_spec: Mapping[str, Any],
                       global_extra: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[Namespace, ...]]:
    """
    The structures ONE unit runs on: its own list if it declared one, the
    connection-level list otherwise.

    Resolves as a GROUP, not element-wise -- the same call `EchoSettings.resolve`
    makes for its opcode keys, and for the same reason. A unit naming any
    structures file is describing its whole link; half-inheriting a
    connection-level module would scope it to a layout set neither peer
    configured.

    Returns `(raw spellings, resolved namespaces)`. Both are kept because a
    filesystem path cannot be recovered from a namespace, and re-deriving the
    namespace at every lookup is exactly the drift this design avoids.
    """
    raw = _structures_from(unit_spec, "connections[...]['Structures']")
    if raw is None:
        raw = _structures_from(global_extra, "Structures") or ()
    return raw, tuple(resolve_module_name(entry) for entry in raw)


@dataclass(frozen=True, slots=True)
class TopicSpec:
    """
    *Immutable class*
    One DDS topic: its name on the wire, the `@idl.struct` class that carries
    it, and which direction this connection runs it in.

    `opcode` is a LOCAL routing handle, never transmitted. DDS puts no opcode
    on the wire -- the topic is the message identity -- but this framework
    routes on `(unit_code, opcode)`, so each topic is given a stable surrogate
    derived from its name by `tools.general.topic_opcode`. That is what makes
    `receive_message`, `handle_on_receive` and `@route` work over DDS at all.

    `type_name` overrides the DDS type name, which otherwise defaults to the
    Python class name. Remote units match on (topic name, type name, QoS), so
    this is the knob for talking to a peer whose type was generated from real
    IDL under a different name (`MyModule::Track`). Usually unset.
    """

    topic: str
    type_ref: str
    direction: TopicDirection
    opcode: OpCode
    type_name: str | None = None

    @property
    def publishes(self) -> bool:
        return self.direction in (TopicDirection.PUBLISH, TopicDirection.BOTH)

    @property
    def subscribes(self) -> bool:
        return self.direction in (TopicDirection.SUBSCRIBE, TopicDirection.BOTH)


def parse_topics(raw: Any, side: Side) -> tuple[TopicSpec, ...]:
    """
    Build the topic list, defaulting each entry's direction from `side`.

    The surrogate opcodes are assigned AND collision-checked here, at load
    time, for the same reason every other config error is raised here: two
    topics sharing a route key is a silent misroute at runtime -- the second
    topic's samples delivered to the first topic's subscriber -- and there is
    no later point at which it would announce itself. Both names are put in
    the message, along with the `opcode` escape hatch that resolves it.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(
            f"config[{TOPICS_KEY!r}] must be a list of topic objects, got {type(raw).__name__}.\n"
            f"[*] e.g. [{{'topic': 'TrackTopic', 'type': 'Track', 'direction': 'subscribe'}}]")

    default_direction = {
        Side.PUBLISHER: TopicDirection.PUBLISH,
        Side.SENDER: TopicDirection.PUBLISH,
        Side.SUBSCRIBER: TopicDirection.SUBSCRIBE,
        Side.RECEIVER: TopicDirection.SUBSCRIBE,
    }.get(side, TopicDirection.BOTH)

    topics: list[TopicSpec] = []
    seen_names: dict[str, int] = {}
    opcode_owner: dict[OpCode, str] = {}
    for index, entry in enumerate(raw):
        where = f"config[{TOPICS_KEY!r}][{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object, got {entry!r}")
        name = _lookup(entry, *TOPIC_NAME_KEYS)
        if not name:
            raise ValueError(f"{where} needs a {TOPIC_NAME_KEYS[0]!r} key naming the DDS topic")
        name = str(name)
        if name in seen_names:
            raise ValueError(
                f"{where}: topic {name!r} is already declared at index {seen_names[name]}")
        seen_names[name] = index
        type_ref = _lookup(entry, *TOPIC_TYPE_KEYS)
        if not type_ref:
            raise ValueError(
                f"{where}: topic {name!r} needs a {TOPIC_TYPE_KEYS[0]!r} key naming its "
                f"@idl.struct class")
        raw_direction = _lookup(entry, *TOPIC_DIRECTION_KEYS)
        try:
            direction = default_direction if raw_direction is None else TopicDirection(
                str(raw_direction).lower())
        except ValueError as exc:
            raise ValueError(
                f"{where}: direction {raw_direction!r} is not one of "
                f"{[d.value for d in TopicDirection]}") from exc
        raw_opcode = _lookup(entry, *TOPIC_OPCODE_KEYS)
        opcode = topic_opcode(name) if raw_opcode is None else _as_opcode(
            raw_opcode, f"{where}['opcode']")
        if opcode in opcode_owner:
            raise ValueError(
                f"{where}: topics {opcode_owner[opcode]!r} and {name!r} both route on opcode "
                f"{opcode:#06x}, so their samples would be indistinguishable to "
                f"receive_message()/@route.\n"
                f"[*] The opcode is derived from the topic NAME (tools.general.topic_opcode) and "
                f"is local only -- nothing is sent on the wire.\n"
                f"[*] Fix by giving either topic an explicit {TOPIC_OPCODE_KEYS[0]!r} key.")
        opcode_owner[opcode] = name
        topics.append(TopicSpec(
            topic=name,
            type_ref=str(type_ref),
            direction=direction,
            opcode=opcode,
            type_name=_lookup(entry, *TOPIC_TYPE_NAME_KEYS),
        ))
    return tuple(topics)


@dataclass(frozen=True, slots=True)
class UnitEndpoint:
    """
    *Immutable class*
    Where one logical unit lives, how it identifies itself on the wire, and
    how it heartbeats.

    `port` is the transport port the unit is reached on (for DDS, the domain
    id). -- names are a configuration-level convenience,
    the UnitCode is what the protocol itself actually uses.

    `echo` is this unit's OWN settings, already merged against the
    connection-level block by `EchoSettings.resolve` resolves at load time.

    `structures` is this LINK's IRS layouts, resolved to module namespaces --
    what scopes every encode/decode/validate for this unit. Empty means
    unscoped: every registered module is searched, which is what a byte-oriented
    unit (and every config written before per-link structures existed) gets.
    `structures_raw` keeps the spellings as configured, because that is what
    `ConnectionManager` hands to `import_modules`.
    """

    port: int
    unitCode: UnitCode
    echo: EchoSettings = field(default_factory=EchoSettings)
    structures_raw: tuple[str, ...] = ()
    structures: tuple[Namespace, ...] = ()


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """
    *Immutable class*
    One unit fill connection's configuration.

    Build these with `from_json()` rather than by hand -- that is where the
    JSON is validated and coerced into real types.
    """
    protocol: Protocol
    side: Side
    ip: str
    local_ip: str
    #: Our unitCode
    unitCode: int
    connections: dict[str, UnitEndpoint]
    #: DDS only, empty everywhere else. Topics are their own axis, independent
    #: of `connections` -- see TopicSpec.
    topics: tuple[TopicSpec, ...] = ()
    #: (Echo-Opcodes, Echo-Timeout, Echo-Intervals, Mode('send_only'/'receive_only'), IDL_file, QoS_file, local_ip, ...)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ConnectionConfig:
        """
        Build a validated config from a raw JSON dict.

        Every failure mode is raised here, at load time.
        """
        own_unitCode_raw = _lookup(data, *UNIT_CODE_KEYS)
        if own_unitCode_raw is None:
            raise ValueError("config['unitCode'] is required: it is this connection's OWN unit code")
        own_unit_code = _as_unit_code(own_unitCode_raw, "unitCode")
        connections_raw = data.get(CONNECTIONS_KEY)
        if not connections_raw:
            raise ValueError(
                f"config[{CONNECTIONS_KEY!r}] is required and must map every connection name to "
                f"{{{PORT_KEY!r}: int, 'unitCode': int}}")
        required_keys = {PROTOCOL_KEY, SIDE_KEY, IP_KEY, *LOCAL_IP_KEYS, CONNECTIONS_KEY,
                         *UNIT_CODE_KEYS, TOPICS_KEY}
        extra = {key: value for key, value in data.items() if key not in required_keys}
        # Parsed up here rather than at construction: the Structures rule below
        # has to know the protocol to exempt multicast, and to name it if it
        # rejects the config.
        protocol = Protocol(str(data[PROTOCOL_KEY]).lower())
        side = Side(str(data[SIDE_KEY]).lower())
        topics = parse_topics(data.get(TOPICS_KEY), side)
        if protocol is not Protocol.DDS and topics:
            raise ValueError(
                f"config[{TOPICS_KEY!r}] is only meaningful on a 'dds' connection; this one is "
                f"{protocol.value!r}.")
        # The echo lifecycle sends a raw `bytes` payload (b'' by default), which
        # a DataWriter cannot accept -- it serializes typed samples through the
        # type's TypeSupport. An echo configured here would therefore never
        # heartbeat, and its watchdog would then drop every unit on
        # EchoTimeout. DDS's own LIVELINESS QoS is the mechanism that belongs
        # in that slot, so this is an error rather than a docstring warning.
        if protocol is Protocol.DDS:
            echo_here = sorted(ECHO_KEYS.intersection(extra))
            echo_in_units = sorted({
                key for spec in connections_raw.values() if isinstance(spec, dict)
                for key in ECHO_KEYS.intersection(spec)})
            if echo_here or echo_in_units:
                raise ValueError(
                    f"echo keys are not supported on a 'dds' connection "
                    f"(found {echo_here + echo_in_units}).\n"
                    f"[*] The echo lifecycle transmits raw bytes, which a DDS DataWriter rejects: "
                    f"it publishes typed samples only.\n"
                    f"[*] Use DDS LIVELINESS QoS in the QoS profile instead -- it is the same "
                    f"mechanism, enforced by the middleware.")
        connection_structures = _structures_from(extra, "Structures")
        connections: dict[str, UnitEndpoint] = {}
        code_owner: dict[UnitCode, str] = {}
        for name, spec in connections_raw.items():
            if not isinstance(spec, dict) or PORT_KEY not in spec or all(unitCode_key not in spec for unitCode_key in UNIT_CODE_KEYS):
                raise ValueError(
                    f"config['connections'][{name!r}] must be an object with at least a {PORT_KEY!r} key, got {spec!r}")
            try:
                port = int(spec[PORT_KEY])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"config['connections'][{name!r}][{PORT_KEY!r}] must be an integer, got {spec[PORT_KEY]!r}") from exc
            if not 0 <= port <= 0xFFFF:
                raise ValueError(
                    f"config['connections'][{name!r}][{PORT_KEY!r}] = {port} is not a valid port\n[*] Has to be between 0 - 65,535.")
            raw_unitCode = _lookup(spec, *UNIT_CODE_KEYS)
            unitCode = _as_unit_code(raw_unitCode, f"connections[{name!r}]['unitCode']")
            if unitCode in code_owner:
                raise ValueError(f"connections {code_owner[unitCode]!r} and {name!r} both use unitCode "
                                 f"{unitCode}; unit codes must be unique within a connection")
            code_owner[unitCode] = name
            # Per-unit echo wins, connection-level `extra` is the fallback.
            try:
                echo = EchoSettings.resolve(spec, extra)
                structures_raw, structures = resolve_structures(spec, extra)
            except ValueError as exc:
                raise ValueError(f"config['connections'][{name!r}]: {exc}") from exc
            connections[name] = UnitEndpoint(port=port, unitCode=unitCode, echo=echo,
                                             structures_raw=structures_raw, structures=structures)

        # A structures file defines ONE link, so a connection-level list is only
        # meaningful when there is one link. With several units it would scope
        # every one of them to the same namespace -- which is precisely how two
        # files that share an opcode used to erase each other. Multicast is the
        # sole exception: one sender fans out to many receivers over one IRS.
        if connection_structures and len(connections) > 1 and protocol is not Protocol.MULTICAST:
            raise ValueError(
                f"config['Structures'] is a connection-level default and is only legal when the "
                f"connection has exactly one unit; this {protocol.value} connection has "
                f"{len(connections)} ({sorted(connections)}).\n"
                f"[*] A structures file defines ONE server<->client link, so every unit here must "
                f"declare its own 'Structures' inside its connections[<name>] entry.\n"
                f"[*] Multicast is the sole exception: one sender fans out to many receivers over "
                f"a single shared IRS.")

        config = cls(
            protocol=protocol,
            side=side,
            ip=data[IP_KEY],
            local_ip=_lookup(data, *LOCAL_IP_KEYS) or "0.0.0.0",
            unitCode=own_unit_code,
            connections=connections,
            topics=topics,
            extra=extra,
        )
        # Parse the echo and structures blocks at load-time, so a malformed key
        # is a load failure rather than a link that silently misbehaves later.
        config.echo
        config.structures
        return config

    # ------------------------------------------------------------------ #
    # Unit lookups
    # ------------------------------------------------------------------ #
    def endpoint_for(self, unit_name: str) -> UnitEndpoint:
        """The endpoint for `unit_name`, or `ValueError` naming what is
        configured -- used wherever a missing unit is a caller error rather
        than an expected miss."""
        endpoint = self.connections.get(unit_name)
        if endpoint is None:
            raise ValueError(
                f"Unknown unit {unit_name!r}; known units: {list(self.connections)}"
            )
        return endpoint

    def unit_from_port(self, port: int) -> str | None:
        """Reverse lookup: which unit listens on `port`, or None."""
        for name, endpoint in self.connections.items():
            if endpoint.port == port:
                return name
        return None

    def port_for_unit(self, unit_name: str) -> int | None:
        endpoint = self.connections.get(unit_name)
        return None if endpoint is None else endpoint.port

    def unit_code_for(self, unit_name: str) -> int:
        """The wire-level unit code for `unit_name`.
         Raises ValueError if unknown."""
        return self.endpoint_for(unit_name).unitCode

    def echo_for(self, unit_name: str) -> EchoSettings:
        """The echo settings for `unit_name`."""
        return self.endpoint_for(unit_name).echo

    def unit_for_code(self, unit_code: UnitCode) -> str | None:
        """Reverse lookup: which unit carries `unit_code`, or None.

        DDS needs this where the framed protocols don't: there, inbound routing
        comes from the transport (which socket a message arrived on), but a DDS
        reader serves every publisher on its topic at once, so the sending unit
        can only be identified from the sample itself."""
        for name, endpoint in self.connections.items():
            if endpoint.unitCode == unit_code:
                return name
        return None

    def topic_for_opcode(self, opcode: OpCode) -> TopicSpec | None:
        """The topic routing on `opcode`, or None."""
        for topic in self.topics:
            if topic.opcode == opcode:
                return topic
        return None

    def structures_for(self, unit_name: str) -> tuple[Namespace, ...]:
        """The IRS structures namespaces scoping this link. Empty == unscoped."""
        return self.endpoint_for(unit_name).structures

    @property
    def unit_codes(self) -> dict[str, int]:
        """returns {unit name: unit code}, for callers that want the whole mapping."""
        return {name: endpoint.unitCode for name, endpoint in self.connections.items()}

    @property
    def connected_units(self) -> list[str]:
        """All logical unit names reachable through this connection instance"""
        return list(self.connections)

    @property
    def ports(self) -> list[int]:
        """Every port this connection binds or dials, in declaration order."""
        return [endpoint.port for endpoint in self.connections.values()]

    @property
    def echo(self) -> EchoSettings:
        """This connection's echo block -- the shared default every unit falls
        back to, parsed straight out of `extra`.
        Recomputed per access rather than cached: `slots=True` leaves no
        instance `__dict__` for a `cached_property` to write into.
        """
        return EchoSettings.from_extra(self.extra)

    @property
    def unit_echoes(self) -> dict[str, EchoSettings]:
        """unit name -> resolved echo settings, for callers that want the
        whole mapping (`base.Connection` caches exactly this at construction)."""
        return {name: endpoint.echo for name, endpoint in self.connections.items()}

    @property
    def structures(self) -> tuple[Namespace, ...]:
        """This connection's own `Structures` block, resolved -- the fallback
        for a unit the config never gave one. Recomputed per access rather than
        cached, same as `echo`: `slots=True` leaves no instance `__dict__`."""
        raw = _structures_from(self.extra, "Structures") or ()
        return tuple(resolve_module_name(entry) for entry in raw)

    @property
    def unit_structures(self) -> dict[str, tuple[Namespace, ...]]:
        """unit name -> resolved structures namespaces (`base.Connection`
        caches exactly this at construction)."""
        return {name: endpoint.structures for name, endpoint in self.connections.items()}

    @property
    def all_structures_raw(self) -> tuple[str, ...]:
        """Every structures spelling this config references -- connection-level
        plus every per-unit list -- de-duplicated in declaration order. This is
        what `ConnectionManager` imports, so a per-unit list is never missed."""
        seen: dict[str, None] = {}
        for entry in _structures_from(self.extra, "Structures") or ():
            seen[entry] = None
        for endpoint in self.connections.values():
            for entry in endpoint.structures_raw:
                seen[entry] = None
        return tuple(seen)
