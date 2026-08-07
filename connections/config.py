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
    "RadarUnit":   {"port": 2000, "unitCode": 7, "echo_opcode": 10},
    "TrackerUnit": {"port": 2001, "unitCode": 8}
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
    identity. It keys the routing tables and is the code handed to the parser
    when decoding what that unit sent us.

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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from tools.general import validated_opCode, validated_unitCode
from annotations import *

DEFAULT_ECHO_INTERVAL: float = 1.0
DEFAULT_ECHO_TIMEOUT: float = 5.0

#: Every json key defined
#PROTOCOL_KEYS = frozenset(("protocol", ""))
UNIT_CODE_KEYS = frozenset(("UnitCode", "unitCode", "unit_code"))

ECHO_OPCODE_KEYS = ("echo_opcode", "EchoOpcode", "echoOpcode")
RECV_ECHO_OPCODE_KEYS = ("recv_echo_opcode", "RecvEchoOpcode", "recvEchoOpcode")
SEND_ECHO_OPCODE_KEYS = ("send_echo_opcode", "SendEchoOpcode", "sendEchoOpcode")
ECHO_OPCODE_KEYS: tuple[str, ...] = (*ECHO_OPCODE_KEYS, *RECV_ECHO_OPCODE_KEYS, *SEND_ECHO_OPCODE_KEYS)

ECHO_INTERVAL_KEYS = ("echo_interval", "EchoInterval", "echoInterval")
ECHO_TIMEOUT_KEYS = ("echo_timeout", "EchoTimeout", "echoTimeout")
ECHO_PAYLOAD_KEYS = ("echo_payload", "EchoPayload", "echoPayload")
ECHO_TUNING_KEYS: tuple[str, ...] = (*ECHO_INTERVAL_KEYS, *ECHO_TIMEOUT_KEYS, *ECHO_PAYLOAD_KEYS)

ECHO_KEYS: frozenset[str] = frozenset(ECHO_OPCODE_KEYS + ECHO_TUNING_KEYS)


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
        opcode = validated_opCode(value)
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
        code = validated_unitCode(value)
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


def _as_payload(value: Any) -> bytes:
    """Echo payloads arrive from JSON, so allow the shapes JSON can express."""
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode()
    if isinstance(value, (list, tuple)):
        return bytes(value)
    raise ValueError(f"config['echo_payload'] must be str/bytes/list[int], got {value!r}")


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
        merged = {k: v for k, v in global_extra.items() if k in ECHO_KEYS}
        if any(unit_spec.get(key) is not None for key in ECHO_OPCODE_KEYS):
            for key in ECHO_OPCODE_KEYS:
                merged.pop(key, None)
        merged.update({k: v for k, v in unit_spec.items() if k in ECHO_KEYS})
        return cls.from_extra(merged)


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
    """

    port: int
    unitCode: UnitCode
    echo: EchoSettings = field(default_factory=EchoSettings)


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
    #: protocol-specific opts (echo opcodes, ttl, mode, idl_file, qos_file, ...)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ConnectionConfig:
        """
        Build a validated config from a raw JSON dict.

        Every failure mode is raised here, at load time, rather than being
        discovered when a socket refuses to open or a message routes to the
        wrong unit: a missing own `unitCode`, missing `connections`, a port or
        unit code out of range, two units claiming the same unit code, or a
        malformed echo block at either level of the hierarchy.
        """
        own_code_raw = _lookup(data, "unitCode", "unit_code")
        if own_code_raw is None:
            raise ValueError(
                "config['unitCode'] is required: it is this connection's OWN unit code, "
                "stamped into every message it sends. The codes inside 'connections' "
                "identify the REMOTE units and cannot stand in for it"
            )
        own_unit_code = _as_unit_code(own_code_raw, "unitCode")

        connections_raw = data.get("connections")
        if not connections_raw:
            raise ValueError(
                "config['connections'] is required and must map every connection name "
                "to {'port': int, 'unitCode': int} -- there is no implicit/'default' unit"
            )

        # `extra` is built before the units are, because each unit's echo block
        # resolves against it (EchoSettings.resolve).
        known_keys = {"protocol", "side", "ip", "local_ip", "connections",
                      "unitCode", "unit_code"}
        extra = {k: v for k, v in data.items() if k not in known_keys}

        connections: dict[str, UnitEndpoint] = {}
        code_owner: dict[int, str] = {}
        for name, spec in connections_raw.items():
            if not isinstance(spec, dict) or "port" not in spec:
                raise ValueError(
                    f"config['connections'][{name!r}] must be an object with at least "
                    f"a 'port' key, got {spec!r}"
                )
            try:
                port = int(spec["port"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"config['connections'][{name!r}]['port'] must be an integer, "
                    f"got {spec['port']!r}"
                ) from exc
            if not 0 <= port <= 0xFFFF:
                raise ValueError(
                    f"config['connections'][{name!r}]['port'] = {port} is not a valid port"
                )

            # The REMOTE unit's code. Optional -- defaults to the port's low
            # byte -- but still range- and collision-checked below.
            raw_code = spec.get("unitCode", spec.get("unit_code"))
            code = (
                port & 0xFF if raw_code is None
                else _as_unit_code(raw_code, f"connections[{name!r}]['unitCode']")
            )
            if code in code_owner:
                # Two units sharing a code would collapse into one route.
                raise ValueError(
                    f"connections {code_owner[code]!r} and {name!r} both use unitCode "
                    f"{code}; unit codes must be unique within a connection"
                )
            code_owner[code] = name

            # Per-unit echo wins, connection-level `extra` is the fallback.
            # Re-raised with the unit named, because "EchoTimeout must be
            # greater than EchoInterval" is unactionable on a config where
            # three units each supply their own.
            try:
                echo = EchoSettings.resolve(spec, extra)
            except ValueError as exc:
                raise ValueError(f"config['connections'][{name!r}]: {exc}") from exc
            connections[name] = UnitEndpoint(port=port, unitCode=code, echo=echo)

        config = cls(
            protocol=Protocol(str(data["protocol"]).lower()),
            side=Side(str(data["side"]).lower()),
            ip=data["ip"],
            local_ip=data.get("local_ip", "0.0.0.0"),
            unitCode=own_unit_code,
            connections=connections,
            extra=extra,
        )
        # Parse the echo block eagerly so a bad EchoInterval/opcode is a
        # config-time error with a clear message, not a surprise at start().
        config.echo
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
        """The wire-level unit code for `unit_name`. Raises if unknown."""
        return self.endpoint_for(unit_name).unitCode

    def echo_for(self, unit_name: str) -> EchoSettings:
        """The echo settings `unit_name` actually heartbeats on -- its own
        keys where it has them, this connection's `extra` block where it
        doesn't, and disabled where neither supplies an opcode. Resolved at
        load time; this is a lookup, not a re-parse."""
        return self.endpoint_for(unit_name).echo

    @property
    def unit_codes(self) -> dict[str, int]:
        """unit name -> unit code, for callers that want the whole mapping."""
        return {name: endpoint.unitCode for name, endpoint in self.connections.items()}

    @property
    def connected_units(self) -> list[str]:
        """All logical unit names reachable through this connection instance,
        derived directly from the explicit `connections` block -- there is no
        "default"/anonymous-unit fallback."""
        return list(self.connections)

    @property
    def ports(self) -> list[int]:
        """Every port this connection binds or dials, in declaration order."""
        return [endpoint.port for endpoint in self.connections.values()]

    @property
    def echo(self) -> EchoSettings:
        """This connection's echo block -- the shared default every unit falls
        back to, parsed straight out of `extra`.

        Use `echo_for(unit)` to get what a *unit* actually runs on; this is
        only the connection-level half of that. It stays a property because
        `from_json` evaluates it once to force a load-time failure on a bad
        `extra` block even when every unit overrides it.

        Recomputed per access rather than cached: `slots=True` leaves no
        instance `__dict__` for a `cached_property` to write into.
        """
        return EchoSettings.from_extra(self.extra)

    @property
    def unit_echoes(self) -> dict[str, EchoSettings]:
        """unit name -> resolved echo settings, for callers that want the
        whole mapping (`base.Connection` caches exactly this at construction)."""
        return {name: endpoint.echo for name, endpoint in self.connections.items()}
