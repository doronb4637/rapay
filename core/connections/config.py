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
keys (ttl, mode, ...) by the individual protocol classes.

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

A `"protocol": "dds"` config is a different shape altogether: a DDS node has no
socket to describe, and its routing is the system contract's to say. It names
WHICH UNIT it is in WHICH DDS Interface, and that is all it must name:

{
  "protocol": "dds",
  "unit": "SensorUnit",
  "dds_interface": "C:/ICD/generated/dds_interface.py"
}

Its own unit code, its peers (`connections`) and its topics are derived from
the Interface (`dds_config.resolve_unit`), and `side` / `ip` / `local_ip` are
None. See `_from_dds_json` for the optional keys and the ones it refuses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from core.tools.general import resolve_module_name, validated_opcode, validated_unitcode
from core.annotations import Namespace, OpCode, UnitCode

# Re-exported: TopicDirection/TopicSpec used to live here, and are still part of
# what a DDS ConnectionConfig hands out (`config.dds.topics`).
from .dds_config import (DEFAULT_DOMAIN_ID, DEFAULT_QOS_FILE, DdsUnitConfig, TopicDirection,
                         TopicSpec, resolve_unit)

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
#: There is deliberately no `echo_payload` here. The heartbeat body is always
#: empty: `UnitEchoSupervisor` sends b"" and nothing ever read a configured
#: value, so the key was accepted, documented, and silently ignored.
ECHO_TUNING_KEYS: tuple[str, ...] = (*ECHO_INTERVAL_KEYS, *ECHO_TIMEOUT_KEYS)

ECHO_KEYS: frozenset[str] = frozenset(ALL_ECHO_OPCODE_KEYS + ECHO_TUNING_KEYS)

#: DDS. A DDS node is configured as ONE UNIT of a DDS Interface; everything else
#: about its routing is the Interface's to say (see `_from_dds_json`).
DDS_UNIT_KEYS = ("unit", "unit_id", "Unit", "unitId")
DDS_INTERFACE_KEYS = ("dds_interface", "DdsInterface", "ddsInterface")
DDS_DOMAIN_ID_KEYS = ("domain_id", "DomainId", "domainId")
DDS_QOS_FILE_KEYS = ("qos_file", "QosFile", "qosFile")
DDS_QOS_PROFILE_KEYS = ("qos_profile", "QosProfile", "qosProfile")
#: The hand-written topic list DDS configs used to carry. Refused everywhere
#: now: DDS topics are the classes in the Interface, and no other protocol has any.
TOPICS_KEY = "topics"


class Protocol(str, Enum):
    TCP = "tcp"
    UDP = "udp"
    MULTICAST = "multicast"
    DDS = "dds"


class Side(str, Enum):
    """Which end of a socket this connection is. DDS has none: what a DDS unit
    publishes and subscribes is per topic, from its DDS Interface, and its
    `ConnectionConfig.side` is always None."""
    # TCP
    CLIENT = "client"
    SERVER = "server"
    # UDP / MULTICAST
    SENDER = "sender"
    RECEIVER = "receiver"


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


def _as_opcode(value: Any, field_name: str) -> OpCode:
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


def _as_unit_code(value: Any, field_name: str) -> UnitCode:
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

          * The three OPCODE keys resolve as a GROUP. A unit naming any of them
            is describing its whole heartbeat, so the connection-level opcodes
            drop out entirely rather than half-applying -- a unit with
            `{"echo_opcode": 10}` under a global `{"recv_echo_opcode": 99}` must
            not end up receiving on 99 and sending on 10, a link neither peer
            configured.
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


# --------------------------------------------------------------------------- #
# The steps of `from_json`, below. Each owns exactly one of this config's
# failure modes and raises it with the message naming the offending key, so the
# classmethod itself reads as the order those checks happen in.
# --------------------------------------------------------------------------- #
def _parse_own_unit_code(data: Mapping[str, Any]) -> UnitCode:
    """OUR code -- who this process is on the wire. Required: there is no
    anonymous-unit fallback."""
    raw = _lookup(data, *UNIT_CODE_KEYS)
    if raw is None:
        raise ValueError("config['unitCode'] is required: it is this connection's OWN unit code")
    return _as_unit_code(raw, "unitCode")


def _require_connections(data: Mapping[str, Any]) -> dict[str, Any]:
    """`connections` is the single source of truth for unit routing -- required,
    with no implicit "default" unit and no separate port list to keep in sync."""
    connections_raw = data.get(CONNECTIONS_KEY)
    if not connections_raw:
        raise ValueError(
            f"config[{CONNECTIONS_KEY!r}] is required and must map every connection name to "
            f"{{{PORT_KEY!r}: int, 'unitCode': int}}")
    return connections_raw


def _split_extra(data: Mapping[str, Any]) -> dict[str, Any]:
    """Everything outside the fixed key set, left for whoever owns it: the echo
    keys for `EchoSettings`, protocol-specific keys (ttl, mode, ...) for the
    individual protocol classes."""
    fixed_keys = {PROTOCOL_KEY, SIDE_KEY, IP_KEY, *LOCAL_IP_KEYS, CONNECTIONS_KEY, *UNIT_CODE_KEYS}
    return {key: value for key, value in data.items() if key not in fixed_keys}


def _reject_topics(data: Mapping[str, Any], protocol: Protocol) -> None:
    """A socket protocol has no topics; the key can only be a mistake, and
    ignoring it silently would hide the real misconfiguration."""
    if TOPICS_KEY in data:
        raise ValueError(
            f"config[{TOPICS_KEY!r}] is not a {protocol.value!r} setting: only DDS has topics, "
            f"and those come from its DDS Interface.")


# --------------------------------------------------------------------------- #
# The steps of `_from_dds_json`.
# --------------------------------------------------------------------------- #
#: Keys a DDS config refuses, each with the reason. A DDS node has no socket to
#: describe, and what used to be written here by hand now comes from the DDS
#: Interface. Refused rather than ignored: a key nothing reads is a setting its
#: author believes is in force.
_DDS_REFUSED_KEYS: dict[tuple[str, ...], str] = {
    (SIDE_KEY,): "DDS has no side -- what a unit publishes and subscribes comes from its "
                 "DdsUnit in the DDS Interface",
    (IP_KEY, *LOCAL_IP_KEYS): "DDS has no endpoint address -- peers find each other by discovery",
    UNIT_CODE_KEYS: "this unit's code comes from its DdsUnit in the DDS Interface",
    (CONNECTIONS_KEY,): "the peers are derived from the DDS Interface",
    (TOPICS_KEY,): "the topics are the classes the unit's DdsUnit publishes and subscribes",
    ("idl_modules", "idl_file"): "the DDS Interface imports its topic classes itself",
    STRUCTURES_KEYS: "Structures are IRS message layouts; DDS samples are typed by their classes",
    tuple(sorted(ECHO_KEYS)): "the echo transmits raw bytes, which a DataWriter cannot publish "
                              "-- use LIVELINESS QoS in the QoS profile instead",
}


def _refuse_non_dds_keys(data: Mapping[str, Any]) -> None:
    found = [(key, reason) for keys, reason in _DDS_REFUSED_KEYS.items() for key in keys if key in data]
    if found:
        reasons = "\n".join(f"[*] {key!r}: {reason}" for key, reason in found)
        raise ValueError(
            f"protocol 'dds' does not accept {[key for key, _ in found]}:\n{reasons}")


def _parse_dds_domain_id(data: Mapping[str, Any]) -> int:
    raw = _lookup(data, *DDS_DOMAIN_ID_KEYS)
    if raw is None:
        return DEFAULT_DOMAIN_ID
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"config['domain_id'] must be a non-negative integer DDS domain id, got {raw!r}")
    return raw


def _parse_dds_qos_file(data: Mapping[str, Any]) -> Path:
    """The QoS file must exist at load, not at start: a missing file is a
    config error, and `create()` is where config errors surface."""
    raw = _lookup(data, *DDS_QOS_FILE_KEYS)
    path = DEFAULT_QOS_FILE if raw is None else Path(str(raw)).resolve()
    if not path.is_file():
        source = "config['qos_file']" if raw is not None else "the default QoS file (DEFAULT_QOS_FILE)"
        raise FileNotFoundError(f"{source} not found: {path}")
    return path


def _parse_dds_qos_profile(data: Mapping[str, Any]) -> str | None:
    """None selects the QoS file's default (`is_default_qos="true"`) profile.
    Whether a named profile exists is checked against the parsed file when the
    connection is built."""
    raw = _lookup(data, *DDS_QOS_PROFILE_KEYS)
    if raw is not None and (not isinstance(raw, str) or not raw.strip()):
        raise ValueError(f"config['qos_profile'] must be a '<Library>::<Profile>' name, got {raw!r}")
    return raw


def _parse_port(unit_name: str, spec: Mapping[str, Any]) -> int:
    """The transport port this unit is reached on -- for DDS, the domain id."""
    try:
        port = int(spec[PORT_KEY])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"config['connections'][{unit_name!r}][{PORT_KEY!r}] must be an integer, got {spec[PORT_KEY]!r}") from exc
    if not 0 <= port <= 0xFFFF:
        raise ValueError(
            f"config['connections'][{unit_name!r}][{PORT_KEY!r}] = {port} is not a valid port\n[*] Has to be between 0 - 65,535.")
    return port


def _parse_unit_endpoint(unit_name: str, spec: Any, global_extra: Mapping[str, Any],
                         code_owner: dict[UnitCode, str]) -> UnitEndpoint:
    """
    One entry of `connections`: where the unit lives, how it identifies itself
    on the wire, and the echo/structures it inherits or overrides.

    `code_owner` maps each unit code already taken to the unit that took it.
    This reads AND extends it, which is how two units sharing one code -- an
    unroutable config -- is caught within a single pass.
    """
    if (not isinstance(spec, dict) or PORT_KEY not in spec
            or all(unitCode_key not in spec for unitCode_key in UNIT_CODE_KEYS)):
        raise ValueError(
            f"config['connections'][{unit_name!r}] must be an object with at least a {PORT_KEY!r} key, got {spec!r}")
    port = _parse_port(unit_name, spec)
    unitCode = _as_unit_code(_lookup(spec, *UNIT_CODE_KEYS), f"connections[{unit_name!r}]['unitCode']")
    if unitCode in code_owner:
        raise ValueError(f"connections {code_owner[unitCode]!r} and {unit_name!r} both use unitCode "
                         f"{unitCode}; unit codes must be unique within a connection")
    code_owner[unitCode] = unit_name
    # Per-unit echo and structures win; connection-level `extra` is the fallback.
    try:
        echo = EchoSettings.resolve(spec, global_extra)
        structures_raw, structures = resolve_structures(spec, global_extra)
    except ValueError as exc:
        raise ValueError(f"config['connections'][{unit_name!r}]: {exc}") from exc
    return UnitEndpoint(port=port, unitCode=unitCode, echo=echo,
                        structures_raw=structures_raw, structures=structures)


def _parse_unit_endpoints(connections_raw: Mapping[str, Any],
                          global_extra: Mapping[str, Any]) -> dict[str, UnitEndpoint]:
    """Every configured unit, in declaration order, with unit codes checked for
    uniqueness across the whole connection."""
    code_owner: dict[UnitCode, str] = {}
    return {
        unit_name: _parse_unit_endpoint(unit_name, spec, global_extra, code_owner)
        for unit_name, spec in connections_raw.items()
    }


def _check_connection_structures_scope(connection_structures: tuple[str, ...] | None,
                                       connections: Mapping[str, UnitEndpoint],
                                       protocol: Protocol) -> None:
    """
    A structures file defines ONE link, so a connection-level list is only
    meaningful when there is one link. With several units it would scope every
    one of them to the same namespace -- which is precisely how two files that
    share an opcode used to erase each other. Multicast is the sole exception:
    one sender fans out to many receivers over one IRS.
    """
    if not connection_structures or len(connections) <= 1 or protocol is Protocol.MULTICAST:
        return
    raise ValueError(
        f"config['Structures'] is a connection-level default and is only legal when the "
        f"connection has exactly one unit; this {protocol.value} connection has "
        f"{len(connections)} ({sorted(connections)}).\n"
        f"[*] A structures file defines ONE server<->client link, so every unit here must "
        f"declare its own 'Structures' inside its connections[<name>] entry.\n"
        f"[*] Multicast is the sole exception: one sender fans out to many receivers over "
        f"a single shared IRS.")


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """
    *Immutable class*
    One unit fill connection's configuration.

    Build these with `from_json()` rather than by hand -- that is where the
    JSON is validated and coerced into real types.
    """
    protocol: Protocol
    #: None on DDS, which has no side.
    side: Side | None
    #: Both None on DDS, which has no endpoint address.
    ip: str | None
    local_ip: str | None
    #: Our unitCode
    unitCode: int
    connections: dict[str, UnitEndpoint]
    #: (Echo-Opcodes, Echo-Timeout, Echo-Intervals, Mode('send_only'/'receive_only'), local_ip, ...)
    extra: dict[str, Any] = field(default_factory=dict)
    #: DDS only: this unit as its DDS Interface defines it -- its topics, peers,
    #: domain and QoS. None everywhere else.
    dds: DdsUnitConfig | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ConnectionConfig:
        """
        Build a validated config from a raw JSON dict.

        Every failure mode is raised here, at load time. The body below is the
        order those checks happen in; each step's own function owns the rule and
        the message that explains it.
        """
        # Protocol first: a DDS config is a different shape altogether.
        protocol = Protocol(str(data[PROTOCOL_KEY]).lower())
        if protocol is Protocol.DDS:
            return cls._from_dds_json(data)

        own_unit_code = _parse_own_unit_code(data)
        connections_raw = _require_connections(data)
        extra = _split_extra(data)
        side = Side(str(data[SIDE_KEY]).lower())
        _reject_topics(data, protocol)

        connection_structures = _structures_from(extra, "Structures")
        connections = _parse_unit_endpoints(connections_raw, extra)
        _check_connection_structures_scope(connection_structures, connections, protocol)

        config = cls(
            protocol=protocol,
            side=side,
            ip=data[IP_KEY],
            local_ip=_lookup(data, *LOCAL_IP_KEYS) or "0.0.0.0",
            unitCode=own_unit_code,
            connections=connections,
            extra=extra,
        )
        # Touch the CONNECTION-level echo and structures blocks too, so a
        # malformed key there is a load failure rather than a link that
        # silently misbehaves later.
        config.echo
        config.structures
        return config

    @classmethod
    def _from_dds_json(cls, data: Mapping[str, Any]) -> ConnectionConfig:
        """
        A DDS node is ONE UNIT of a DDS Interface. Its unit code, its peers and
        its topics are the Interface's to say (`dds_config.resolve_unit`); this
        config only picks the unit and may override the deployment defaults
        (`domain_id`, `qos_file`, `qos_profile`). `header` stays in `extra` for
        `DdsConnection`, unchanged.

        Each derived peer's `port` is the domain id, which keeps "a DDS
        endpoint's port is its domain" true for anything reading `ports`.
        """
        _refuse_non_dds_keys(data)
        unit = _lookup(data, *DDS_UNIT_KEYS)
        interface = _lookup(data, *DDS_INTERFACE_KEYS)
        if not unit or not interface:
            raise ValueError(
                f"protocol 'dds' needs {DDS_UNIT_KEYS[0]!r} (which DdsUnit this node is) and "
                f"{DDS_INTERFACE_KEYS[0]!r} (the path to the generated DDS Interface); got "
                f"unit={unit!r}, dds_interface={interface!r}")
        dds = resolve_unit(
            str(interface), str(unit),
            domain_id=_parse_dds_domain_id(data),
            qos_file=_parse_dds_qos_file(data),
            qos_profile=_parse_dds_qos_profile(data),
        )
        return cls(
            protocol=Protocol.DDS,
            side=None,
            ip=None,
            local_ip=None,
            unitCode=dds.unit_code,
            connections={name: UnitEndpoint(port=dds.domain_id, unitCode=code) for name, code in dds.peers},
            extra={key: value for key, value in data.items() if key != PROTOCOL_KEY},
            dds=dds,
        )

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
