"""
JSON-driven configuration objects for connections.

Example JSON (a TCP server multiplexing two units over two ports):

{
  "protocol": "tcp",
  "side": "server",
  "ip": "0.0.0.0",
  "local_ip": "0.0.0.0",
  "ports": [5000, 5001],
  "units": {"5000": "RadarUnit", "5001": "TrackerUnit"},
  "echo_opcode": 99,
  "EchoInterval": 1.0,
  "EchoTimeout": 5.0
}

"units" maps each port (as a JSON string key, since JSON object keys are
always strings) to the logical unit name connected on that port. This
mapping is REQUIRED -- there is no implicit/"default" unit name.

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
# Small typed coercion helpers for `extra`, which is raw JSON (`dict[str, Any]`)
# and therefore completely untyped until someone validates it. Doing the
# coercion here -- once, at config time -- means the rest of the codebase can
# assume real `int` / `float` / `bytes` values and never re-check.
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
class EchoSettings:
    """
    Everything the echo lifecycle needs, parsed and validated once.

    Two ways to configure the opcodes:

      * `"echo_opcode": 99`               -- one opcode used for BOTH directions
                                             (the ordinary heartbeat case)
      * `"recv_echo_opcode": 99,`         -- distinct request/reply opcodes
        `"send_echo_opcode": 100`

    A shared `echo_opcode` may still be overridden in one direction by also
    supplying `recv_echo_opcode` or `send_echo_opcode`.

    Whether the two opcodes are equal is not cosmetic -- it changes the
    behaviour of automatic echo handling, see `single_opcode` below.
    """

    recv_opcode: int | None = None
    send_opcode: int | None = None
    interval: float = DEFAULT_ECHO_INTERVAL
    timeout: float = DEFAULT_ECHO_TIMEOUT
    payload: bytes = b""

    @property
    def enabled(self) -> bool:
        """Echo machinery only activates when BOTH directions are known."""
        return self.recv_opcode is not None and self.send_opcode is not None

    @property
    def single_opcode(self) -> bool:
        """
        True when receive and send echo opcodes are the same -- i.e. a plain
        symmetric heartbeat.

        This case MUST NOT auto-reply on receipt: both peers would answer each
        other's answer forever, saturating the link (an echo storm). The
        periodic sender is already keeping the peer informed, so an inbound
        echo only refreshes liveness. When the opcodes differ, the config is
        describing a request/reply pair and auto-replying is exactly right.
        """
        return self.enabled and self.recv_opcode == self.send_opcode

    @classmethod
    def from_extra(cls, extra: dict[str, Any]) -> EchoSettings:
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


@dataclass
class ConnectionConfig:
    protocol: Protocol
    side: Side
    ip: str
    local_ip: str
    ports: list[int]
    unit_map: dict[int, str]                             # port -> unit name; ALWAYS explicit, no fallback
    extra: dict[str, Any] = field(default_factory=dict)    # protocol-specific opts (echo opcodes, ttl, idl_file, ...)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ConnectionConfig:
        ports_raw = data["ports"]
        ports = ports_raw if isinstance(ports_raw, list) else [ports_raw]

        if not data.get("units"):
            raise ValueError(
                "config['units'] is required and must explicitly map every "
                "port to a unit name -- there is no implicit/'default' unit"
            )
        units_raw: dict[str, str] = data["units"]
        unit_map = {int(port_str): unit_name for port_str, unit_name in units_raw.items()}

        known_keys = {"protocol", "side", "ip", "local_ip", "ports", "units"}
        extra = {k: v for k, v in data.items() if k not in known_keys}

        config = cls(
            protocol=Protocol(str(data["protocol"]).lower()),
            side=Side(str(data["side"]).lower()),
            ip=data["ip"],
            local_ip=data.get("local_ip", "0.0.0.0"),
            ports=ports,
            unit_map=unit_map,
            extra=extra,
        )
        # Parse the echo block eagerly so a bad EchoInterval/opcode is a
        # config-time error with a clear message, not a surprise at start().
        config.echo
        return config

    def unit_from_port(self, port: int) -> str | None:
        return self.unit_map.get(port)

    def port_for_unit(self, unit_name: str) -> int | None:
        for port, name in self.unit_map.items():
            if name == unit_name:
                return port
        return None

    @property
    def connected_units(self) -> list[str]:
        """All logical unit names reachable through this connection instance,
        derived directly from the explicit `unit_map` -- there is no
        "default"/anonymous-unit fallback."""
        return list(self.unit_map.values())

    @property
    def echo(self) -> EchoSettings:
        """Echo lifecycle configuration, parsed out of `extra` on demand."""
        return EchoSettings.from_extra(self.extra)
