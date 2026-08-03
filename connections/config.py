"""
JSON-driven configuration objects for connections.

Example JSON (a TCP server multiplexing two units over two ports):

{
  "protocol": "tcp",
  "side": "server",
  "ip": "0.0.0.0",
  "local_ip": "0.0.0.0",
  "connections": {
    "RadarUnit":   {"port": 5000, "unitCode": 7},
    "TrackerUnit": {"port": 5001, "unitCode": 8}
  },
  "echo_opcode": 99,
  "EchoInterval": 1.0,
  "EchoTimeout": 5.0
}

`"connections"` is the single source of truth for unit routing: it maps each
connection name (the logical unit) to the port it lives on and the numeric
unit code it identifies itself with on the wire. It is REQUIRED -- there is
no implicit/"default" unit, and no separate port list to keep in sync with
it.

Everything not in the fixed key set above lands in `extra` and is parsed by
whoever owns it: the echo keys by `EchoSettings` (below), protocol-specific
keys (ttl, mode, idl_file, qos_file, ...) by the individual protocol classes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Defaults for the periodic-echo machinery implemented in base.Connection.
DEFAULT_ECHO_INTERVAL: float = 1.0
DEFAULT_ECHO_TIMEOUT: float = 5.0


class Protocol(str, Enum):
    TCP = "tcp"
    UDP = "udp"
    MULTICAST = "multicast"
    DDS = "dds"


class Side(str, Enum):
    #TCP
    CLIENT = "client"
    SERVER = "server"
    # DDS
    PUBLISHER = "publisher"
    SUBSCRIBER = "subscriber"
    # UDP / MULTICAST
    SENDER = "sender"
    RECEIVER = "receiver"


# --------------------------------------------------------------------------- #
# Small typed coercion helpers for raw JSON (`dict[str, Any]`), which is
# completely untyped until someone validates it. Doing the coercion here --
# once, at config time -- means the rest of the codebase can assume real
# `int` / `float` / `bytes` values and never re-check.
# --------------------------------------------------------------------------- #
def _lookup(extra: dict[str, Any], *names: str) -> Any | None:
    """First present, non-None value among `names`. Every echo key is accepted
    in both the snake_case spelling used by the rest of this config and the
    PascalCase spelling (`EchoInterval`, `EchoTimeout`) used by the interface
    spec, so neither style is a silent no-op."""
    for name in names:
        value = extra.get(name)
        if value is not None:
            return value
    return None


def _as_opcode(value: Any, field_name: str) -> int:
    try:
        opcode = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"config['{field_name}'] must be an integer opcode, got {value!r}"
        ) from exc
    if not 0 <= opcode <= 0xFFFF:
        raise ValueError(
            f"config['{field_name}'] = {opcode} does not fit the uint16 OpCode header field"
        )
    return opcode


def _as_positive_float(value: Any, field_name: str, default: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"config['{field_name}'] must be a number of seconds, got {value!r}"
        ) from exc
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
class UnitEndpoint:
    """
    Where one logical unit lives, and how it identifies itself on the wire.

    `port` is the transport port the unit is reached on (for DDS, the domain
    id). `unit_code` is the value stamped into the uint8 UnitCode field of
    every framed message, and is the identity every routing table in this
    framework keys on -- names are a configuration-level convenience, the
    code is what the protocol itself actually uses.
    """

    port: int
    unit_code: int


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
    """

    recv_opcode: int | None = None
    send_opcode: int | None = None
    interval: float = DEFAULT_ECHO_INTERVAL
    timeout: float = DEFAULT_ECHO_TIMEOUT
    payload: bytes = b""

    @property
    def enabled(self) -> bool:
        """Whether the echo machinery should run at all.

        Requires BOTH directions to be known: a connection that only knew
        which opcode to send could never satisfy its own watchdog, and one
        that only knew which to receive would be watchdogged by a peer it
        never answers.
        """
        return self.recv_opcode is not None and self.send_opcode is not None

    @classmethod
    def from_extra(cls, extra: dict[str, Any]) -> EchoSettings:
        """Parse the echo block out of a config's `extra` dict, raising
        `ValueError` on anything malformed so a typo is a load-time failure
        rather than a link that silently never heartbeats."""
        shared = _lookup(extra, "echo_opcode", "EchoOpcode")
        recv = _lookup(extra, "recv_echo_opcode", "RecvEchoOpcode")
        send = _lookup(extra, "send_echo_opcode", "SendEchoOpcode")

        # A single `echo_opcode` fills in whichever direction wasn't given
        # explicitly, so `{"echo_opcode": 99}` is the whole configuration.
        if shared is not None:
            if recv is None:
                recv = shared
            if send is None:
                send = shared

        interval = _as_positive_float(
            _lookup(extra, "echo_interval", "EchoInterval"), "EchoInterval", DEFAULT_ECHO_INTERVAL
        )
        timeout = _as_positive_float(
            _lookup(extra, "echo_timeout", "EchoTimeout"), "EchoTimeout", DEFAULT_ECHO_TIMEOUT
        )
        if timeout <= interval:
            raise ValueError(
                f"EchoTimeout ({timeout}s) must be greater than EchoInterval ({interval}s), "
                f"otherwise the link is declared dead before the next echo is even due"
            )

        return cls(
            recv_opcode=None if recv is None else _as_opcode(recv, "recv_echo_opcode"),
            send_opcode=None if send is None else _as_opcode(send, "send_echo_opcode"),
            interval=interval,
            timeout=timeout,
            payload=_as_payload(_lookup(extra, "echo_payload", "EchoPayload")),
        )


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """
    One connection's full configuration, immutable once built.

    `frozen=True` because a live `Connection` derives cached state from this
    at construction (unit codes, echo settings) and hands the object to its
    read loops on the event-loop thread: mutating it afterwards would leave
    those caches describing a configuration that no longer exists.
    `slots=True` drops the per-instance `__dict__`, which matters because one
    of these exists per connection and they are read on the message hot path.

    Build these with `from_json()` rather than by hand -- that is where the
    JSON is validated and coerced into real types.
    """

    protocol: Protocol
    side: Side
    ip: str
    local_ip: str
    #: connection/unit name -> where it lives. The single source of truth for
    #: unit routing; there is no separate port list to keep in sync with it.
    connections: dict[str, UnitEndpoint]
    #: protocol-specific opts (echo opcodes, ttl, mode, idl_file, qos_file, ...)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ConnectionConfig:
        """
        Build a validated config from a raw JSON dict.

        Every failure mode is raised here, at load time, rather than being
        discovered when a socket refuses to open or a message routes to the
        wrong unit: missing `connections`, a port or unit code out of range,
        or two units claiming the same unit code.
        """
        connections_raw = data.get("connections")
        if not connections_raw:
            raise ValueError(
                "config['connections'] is required and must map every connection name "
                "to {'port': int, 'unitCode': int} -- there is no implicit/'default' unit"
            )

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

            # unitCode is optional: the low byte of the port is a simple,
            # deterministic default. It is still range- and collision-checked
            # below, so an accidental clash is caught either way.
            raw_code = spec.get("unitCode", spec.get("unit_code"))
            code = port & 0xFF if raw_code is None else int(raw_code)
            if not 0 <= code <= 0xFF:
                raise ValueError(
                    f"config['connections'][{name!r}]['unitCode'] = {code} does not fit "
                    f"the uint8 UnitCode header field"
                )
            if code in code_owner:
                # Two units sharing a code would collapse into one routing
                # slot and silently deliver one unit's traffic to the other.
                raise ValueError(
                    f"connections {code_owner[code]!r} and {name!r} both use unitCode "
                    f"{code}; unit codes must be unique within a connection"
                )
            code_owner[code] = name
            connections[name] = UnitEndpoint(port=port, unit_code=code)

        known_keys = {"protocol", "side", "ip", "local_ip", "connections"}
        extra = {k: v for k, v in data.items() if k not in known_keys}

        config = cls(
            protocol=Protocol(str(data["protocol"]).lower()),
            side=Side(str(data["side"]).lower()),
            ip=data["ip"],
            local_ip=data.get("local_ip", "0.0.0.0"),
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
        return self.endpoint_for(unit_name).unit_code

    @property
    def unit_codes(self) -> dict[str, int]:
        """unit name -> unit code, for callers that want the whole mapping."""
        return {name: endpoint.unit_code for name, endpoint in self.connections.items()}

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
        """Echo lifecycle configuration, parsed out of `extra` on demand.

        Recomputed per access rather than cached: `slots=True` leaves no
        instance `__dict__` for a `cached_property` to write into, and the
        one consumer (`Connection.__init__`) reads it exactly once.
        """
        return EchoSettings.from_extra(self.extra)
