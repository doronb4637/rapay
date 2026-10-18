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
a structures file defines the IRS for ONE link (see connections/CLAUDE.md):
each unit names its own, and a connection-level list is only accepted when
there is exactly one unit to apply it to -- or on multicast, where one sender
genuinely does fan out to many receivers over a single shared IRS. Every unit
MUST end up with a non-empty list: there is no global registry to fall back on,
so a link without one could neither send nor receive.

A unit's name and code are checked against its structures file when the
connection is built (`validate_interfaces`): `"DTU": {"unitCode": 2}` requires
the file to define `DTU = 2`, and our own top-level `unitCode` must be one of
that file's units too.

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

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from core.IRS.irs_parser import Interface
from core.tools.general import import_modules, resolve_module_name, validated_opcode, validated_unitcode
from core.annotations import Namespace, OpCode, UnitCode

from .dds_config import DEFAULT_DOMAIN_ID, DEFAULT_QOS_FILE, DdsUnitConfig, resolve_unit

logger = logging.getLogger("connmgr.config")

DEFAULT_ECHO_INTERVAL: float = 1.0
DEFAULT_ECHO_TIMEOUT: float = 5.0

PROTOCOL_KEY = "protocol"
SIDE_KEY = "side"
IP_KEY = "ip"
CONNECTIONS_KEY = "connections"
PORT_KEY = "port"
UNIT_CODE_KEYS = ("UnitCode", "unitCode", "unit_code")
LOCAL_IP_KEYS = ("local_ip", "localIp")
#: Accepted per unit and connection-wide; the latter only with one link (see `from_json`).
STRUCTURES_KEYS = ("Structures", "structures")

#: The shared "one opcode, both directions" spelling only: a lone recv/send key
#: must never be read as the shared value, so `from_extra` cannot use the union below.
ECHO_OPCODE_KEYS = ("echo_opcode", "EchoOpcode", "echoOpcode")
RECV_ECHO_OPCODE_KEYS = ("recv_echo_opcode", "RecvEchoOpcode", "recvEchoOpcode")
SEND_ECHO_OPCODE_KEYS = ("send_echo_opcode", "SendEchoOpcode", "sendEchoOpcode")
#: Any spelling of any opcode key; `resolve()` treats them as one group.
ALL_ECHO_OPCODE_KEYS: tuple[str, ...] = (*ECHO_OPCODE_KEYS, *RECV_ECHO_OPCODE_KEYS, *SEND_ECHO_OPCODE_KEYS)

ECHO_INTERVAL_KEYS = ("echo_interval", "EchoInterval", "echoInterval")
ECHO_TIMEOUT_KEYS = ("echo_timeout", "EchoTimeout", "echoTimeout")
#: No `echo_payload` key: echo payload is always empty(b"").
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
#: The JSON-schema pointer an editor may add; never a setting.
SCHEMA_KEY = "$schema"

#: Every key a unit's entry in `connections` may carry.
UNIT_KEYS: frozenset[str] = frozenset({PORT_KEY, *UNIT_CODE_KEYS, *STRUCTURES_KEYS, *ECHO_KEYS})


class TransportProtocol(str, Enum):
    """The `"protocol"` values a connection config accepts (case-insensitive)."""
    TCP = "tcp"
    UDP = "udp"
    MULTICAST = "multicast"
    DDS = "dds"


class Side(str, Enum):
    """Which end of a socket this connection is. DDS has none: what a DDS unit
    publishes and subscribes is per topic, from its DDS Interface, and its
    `ConnectionConfig.side` is always None."""
    # TCP / UDP (and a duplex multicast member)
    CLIENT = "client"
    SERVER = "server"
    # MULTICAST: send-only / receive-only
    SENDER = "sender"
    RECEIVER = "receiver"


#: The sides each socket protocol accepts.
_SIDES: dict[TransportProtocol, frozenset[Side]] = {
    TransportProtocol.TCP: frozenset({Side.CLIENT, Side.SERVER}),
    TransportProtocol.UDP: frozenset({Side.CLIENT, Side.SERVER}),
    TransportProtocol.MULTICAST: frozenset(Side),
}
#: Top-level keys only one socket protocol reads.
_PROTOCOL_KEYS: dict[TransportProtocol, frozenset[str]] = {
    TransportProtocol.TCP: frozenset(),
    TransportProtocol.UDP: frozenset({"mode"}),
    TransportProtocol.MULTICAST: frozenset({"ttl"}),
}


# --------------------------------------------------------------------------- #
# Typed coercion helpers: coerce raw JSON once here so nothing downstream re-checks.
# --------------------------------------------------------------------------- #
def _lookup(source: Mapping[str, Any], *names: str) -> Any | None:
    """The first non-null value among the accepted spellings of one key."""
    for name in names:
        value = source.get(name)
        if value is not None:
            return value
    return None


def _as_opcode(value: Any, field_name: str) -> OpCode:
    """Coerce via `tools.general.validated_opcode` (int or "0x.." string) and
    check it fits the uint16 header field."""
    try:
        opcode = validated_opcode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config['{field_name}'] must be an integer opcode, got {value!r}") from exc
    if not 0 <= opcode <= 0xFFFF:
        raise ValueError(f"config['{field_name}'] = {opcode} does not fit the UInt16 OpCode header field")
    return opcode


def _as_unit_code(value: Any, field_name: str) -> UnitCode:
    """Coerce via `tools.general.validated_unitcode` (int or "0x.." string) and
    check it fits the uint8 header field."""
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

        A unit opts out of a connection-level heartbeat with an explicit `null`
        on any opcode key: present-but-null still names the group, so the
        connection-level opcodes drop out whatever spelling they used.
        """
        merged = {key: value for key, value in global_extra.items() if key in ECHO_KEYS}
        if any(key in unit_spec for key in ALL_ECHO_OPCODE_KEYS):
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
    Where one logical unit lives, how it identifies itself on the wire, and
    how it heartbeats.

    `port` is the transport port the unit is reached on (for DDS, the domain
    id). `echo` is this unit's settings, already merged with the
    connection-level block by `EchoSettings.resolve`.

    `structures` is this LINK's IRS structures files, resolved to module
    namespaces; never empty on a socket protocol. `structures_raw` keeps the
    spellings as configured, because that is what `load_interfaces` hands to
    `import_modules`.
    """

    port: int
    unitCode: UnitCode
    echo: EchoSettings = field(default_factory=EchoSettings)
    structures_raw: tuple[str, ...] = ()
    structures: tuple[Namespace, ...] = ()


# --------------------------------------------------------------------------- #
# The steps of `from_json`: each owns one failure mode and names the offending key.
# --------------------------------------------------------------------------- #
def _parse_own_unit_code(data: Mapping[str, Any]) -> UnitCode:
    """OUR code -- who this process is on the wire. Required: there is no
    anonymous-unit fallback."""
    raw = _lookup(data, *UNIT_CODE_KEYS)
    if raw is None:
        raise ValueError("config['unitCode'] is required: it is this connection's OWN unit code")
    return _as_unit_code(raw, "unitCode")


def _require(data: Mapping[str, Any], key: str, meaning: str) -> Any:
    """A required top-level key, as a load-time ValueError rather than a KeyError."""
    value = data.get(key)
    if value is None:
        raise ValueError(f"config[{key!r}] is required: {meaning}")
    return value


def _parse_side(data: Mapping[str, Any], protocol: TransportProtocol) -> Side:
    raw = _require(data, SIDE_KEY, f"which end of the {protocol.value} link this is")
    try:
        side = Side(str(raw).lower())
    except ValueError:
        side = None
    allowed = _SIDES[protocol]
    if side not in allowed:
        raise ValueError(
            f"config[{SIDE_KEY!r}] = {raw!r} is not a {protocol.value} side; "
            f"expected one of {sorted(s.value for s in allowed)}")
    return side


def _warn_unknown_keys(data: Mapping[str, Any], protocol: TransportProtocol) -> None:
    """Top-level `extra` stays open (see `core/configs/connection.schema.json`),
    but a key nothing reads is almost always a typo, so say so."""
    known = {PROTOCOL_KEY, SIDE_KEY, IP_KEY, *LOCAL_IP_KEYS, CONNECTIONS_KEY, *UNIT_CODE_KEYS,
             *STRUCTURES_KEYS, *ECHO_KEYS, SCHEMA_KEY, *_PROTOCOL_KEYS[protocol]}
    unknown = sorted(key for key in data if key not in known)
    if unknown:
        logger.warning("%s config: ignoring unknown key(s) %s", protocol.value, unknown)


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


def _reject_topics(data: Mapping[str, Any], protocol: TransportProtocol) -> None:
    """A socket protocol has no topics; the key can only be a mistake, and
    ignoring it silently would hide the real misconfiguration."""
    if TOPICS_KEY in data:
        raise ValueError(
            f"config[{TOPICS_KEY!r}] is not a {protocol.value!r} setting: only DDS has topics, "
            f"and those come from its DDS Interface.")


# --------------------------------------------------------------------------- #
# The steps of `_from_dds_json`.
# --------------------------------------------------------------------------- #
#: Keys a DDS config refuses, with the reason. Refused rather than ignored: a key
#: nothing reads is a setting its author believes is in force.
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
    ("header",): "the sender is always read from the sample's A_sourceID.A_systemId; there is "
                 "nothing to configure",
}

#: Every key a DDS config may carry.
_DDS_KEYS = frozenset({PROTOCOL_KEY, SCHEMA_KEY, *DDS_UNIT_KEYS, *DDS_INTERFACE_KEYS,
                       *DDS_DOMAIN_ID_KEYS, *DDS_QOS_FILE_KEYS, *DDS_QOS_PROFILE_KEYS})


def _refuse_non_dds_keys(data: Mapping[str, Any]) -> None:
    found = [(key, reason) for keys, reason in _DDS_REFUSED_KEYS.items() for key in keys if key in data]
    if found:
        reasons = "\n".join(f"[*] {key!r}: {reason}" for key, reason in found)
        raise ValueError(
            f"protocol 'dds' does not accept {[key for key, _ in found]}:\n{reasons}")
    unknown = sorted(key for key in data if key not in _DDS_KEYS)
    if unknown:
        raise ValueError(f"protocol 'dds' has no setting(s) {unknown}; accepted: {sorted(_DDS_KEYS)}")


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
    """The transport port this unit is reached on: an int, or a string of one."""
    raw = spec[PORT_KEY]
    try:
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise TypeError
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"config['connections'][{unit_name!r}][{PORT_KEY!r}] must be an integer, got {raw!r}") from exc
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
            f"config['connections'][{unit_name!r}] must be an object with {PORT_KEY!r} and "
            f"'unitCode' keys, got {spec!r}")
    unknown = sorted(key for key in spec if key not in UNIT_KEYS)
    if unknown:
        raise ValueError(
            f"config['connections'][{unit_name!r}] has no setting(s) {unknown}; accepted: {sorted(UNIT_KEYS)}")
    port = _parse_port(unit_name, spec)
    unitCode = _as_unit_code(_lookup(spec, *UNIT_CODE_KEYS), f"connections[{unit_name!r}]['unitCode']")
    if unitCode in code_owner:
        raise ValueError(f"connections {code_owner[unitCode]!r} and {unit_name!r} both use unitCode "
                         f"{unitCode}; unit codes must be unique within a connection")
    code_owner[unitCode] = unit_name
    try:
        echo = EchoSettings.resolve(spec, global_extra)
        structures_raw, structures = resolve_structures(spec, global_extra)
    except ValueError as exc:
        raise ValueError(f"config['connections'][{unit_name!r}]: {exc}") from exc
    if not structures_raw:
        raise ValueError(
            f"config['connections'][{unit_name!r}] has no 'Structures': every link must name "
            f"the IRS structures file(s) it speaks, in its own entry (or once at connection "
            f"level when there is a single unit, or on multicast)")
    return UnitEndpoint(port=port, unitCode=unitCode, echo=echo,
                        structures_raw=structures_raw, structures=structures)


def _parse_unit_endpoints(connections_raw: Mapping[str, Any],
                          global_extra: Mapping[str, Any]) -> dict[str, UnitEndpoint]:
    """Every configured unit, in declaration order, with unit codes and ports
    checked for uniqueness across the whole connection."""
    code_owner: dict[UnitCode, str] = {}
    endpoints = {
        unit_name: _parse_unit_endpoint(unit_name, spec, global_extra, code_owner)
        for unit_name, spec in connections_raw.items()
    }
    port_owner: dict[int, str] = {}
    for unit_name, endpoint in endpoints.items():
        if endpoint.port in port_owner:
            raise ValueError(f"connections {port_owner[endpoint.port]!r} and {unit_name!r} both use port "
                             f"{endpoint.port}; a port identifies one unit")
        port_owner[endpoint.port] = unit_name
    return endpoints


def _check_connection_structures_scope(connection_structures: tuple[str, ...] | None,
                                       connections: Mapping[str, UnitEndpoint],
                                       protocol: TransportProtocol) -> None:
    """
    A structures file defines ONE link, so a connection-level list is only
    meaningful when there is one link. With several units it would scope every
    one of them to the same namespace -- which is precisely how two files that
    share an opcode used to erase each other. Multicast is the sole exception:
    one sender fans out to many receivers over one IRS.
    """
    if not connection_structures or len(connections) <= 1 or protocol is TransportProtocol.MULTICAST:
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
    """One connection's validated configuration. Build it with `from_json()`."""
    protocol: TransportProtocol
    #: None on DDS, which has no side.
    side: Side | None
    #: Both None on DDS, which has no endpoint address.
    ip: str | None
    local_ip: str | None
    #: OUR Unit Code
    unitCode: int
    connections: dict[str, UnitEndpoint]
    #: Every key outside the fixed set: echo, Structures, and protocol-specific
    #: keys (`mode`, `ttl`, ...).
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
        raw_protocol = _require(data, PROTOCOL_KEY, f"one of {[p.value for p in TransportProtocol]}")
        try:
            protocol = TransportProtocol(str(raw_protocol).lower())
        except ValueError:
            raise ValueError(f"config[{PROTOCOL_KEY!r}] = {raw_protocol!r} is not one of "
                             f"{[p.value for p in TransportProtocol]}") from None
        if protocol is TransportProtocol.DDS:
            return cls._from_dds_json(data)

        own_unit_code = _parse_own_unit_code(data)
        connections_raw = _require_connections(data)
        extra = _split_extra(data)
        side = _parse_side(data, protocol)
        ip = _require(data, IP_KEY, "the peer's (or multicast group's) address")
        _reject_topics(data, protocol)
        _warn_unknown_keys(data, protocol)

        connection_structures = _structures_from(extra, "Structures")
        connections = _parse_unit_endpoints(connections_raw, extra)
        _check_connection_structures_scope(connection_structures, connections, protocol)

        config = cls(
            protocol=protocol,
            side=side,
            ip=ip,
            local_ip=_lookup(data, *LOCAL_IP_KEYS) or "0.0.0.0",
            unitCode=own_unit_code,
            connections=connections,
            extra=extra,
        )
        EchoSettings.from_extra(extra)  # fail the load on a malformed connection-level echo block
        return config

    @classmethod
    def _from_dds_json(cls, data: Mapping[str, Any]) -> ConnectionConfig:
        """
        A DDS node is ONE UNIT of a DDS Interface. Its unit code, its peers and
        its topics are the Interface's to say (`dds_config.resolve_unit`); this
        config only picks the unit and may override the deployment defaults
        (`domain_id`, `qos_file`, `qos_profile`).

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
            protocol=TransportProtocol.DDS,
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
    def unit_for_code(self, unit_code: UnitCode) -> str | None:
        """Which unit carries `unit_code`, or None. DDS needs it: a reader serves
        every publisher of its topic, so only the sample can name the sender."""
        for name, endpoint in self.connections.items():
            if endpoint.unitCode == unit_code:
                return name
        return None

    @property
    def unit_codes(self) -> dict[str, int]:
        """{unit name: unit code}."""
        return {name: endpoint.unitCode for name, endpoint in self.connections.items()}

    @property
    def unit_names(self) -> list[str]:
        """Every configured unit name, in declaration order -- configured, not
        necessarily connected (see `Connection.active_units`)."""
        return list(self.connections)

    @property
    def echo(self) -> EchoSettings:
        """The connection-level echo block every unit falls back to. Recomputed
        per access: `slots=True` leaves no `__dict__` for `cached_property`."""
        return EchoSettings.from_extra(self.extra)

    @property
    def unit_echoes(self) -> dict[str, EchoSettings]:
        """{unit name: resolved echo settings}."""
        return {name: endpoint.echo for name, endpoint in self.connections.items()}

    @property
    def unit_structures(self) -> dict[str, tuple[Namespace, ...]]:
        """{unit name: resolved structures namespaces}."""
        return {name: endpoint.structures for name, endpoint in self.connections.items()}


# --------------------------------------------------------------------------- #
# Interfaces: each link's structures, imported and checked against the config.
# --------------------------------------------------------------------------- #
def load_interfaces(config: ConnectionConfig) -> dict[str, Interface]:
    """{unit name: that link's `Interface`}, importing each structures file once.
    Empty on DDS, whose interface is its DDS Interface."""
    if config.protocol is TransportProtocol.DDS:
        return {}
    loaded: dict[str, Interface] = {}
    interfaces: dict[str, Interface] = {}
    for unit_name, endpoint in config.connections.items():
        for raw in endpoint.structures_raw:
            if raw not in loaded:
                loaded[raw] = Interface.from_module(import_modules(raw)[0])
        interfaces[unit_name] = Interface.merge(*(loaded[raw] for raw in endpoint.structures_raw))
    validate_interfaces(config, interfaces)
    return interfaces


def validate_interfaces(config: ConnectionConfig, interfaces: Mapping[str, Interface]) -> None:
    """
    Every configured unit must be a unit of its own structures file, under the
    same name and code, and so must we.

    Unit names are the contract between a config and a structures file: a
    `"DTU": {"unitCode": 3}` against a file defining `DTU = 2` would subscribe
    and parse against the wrong sender, silently.
    """
    for unit_name, endpoint in config.connections.items():
        interface = interfaces[unit_name]
        where = f"structures {', '.join(interface.namespaces)}"
        defined = interface.units.get(unit_name)
        if defined is None:
            raise ValueError(
                f"config['connections'][{unit_name!r}]: {where} defines no unit named "
                f"{unit_name!r}; units defined there: {dict(interface.units)}")
        if defined != endpoint.unitCode:
            raise ValueError(
                f"config['connections'][{unit_name!r}]['unitCode'] = {endpoint.unitCode}, but "
                f"{where} defines {unit_name} = {defined}")
        if config.unitCode not in interface.units.values():
            raise ValueError(
                f"config['unitCode'] = {config.unitCode} is not a unit of {where} (the link to "
                f"{unit_name!r}); units defined there: {dict(interface.units)}")
