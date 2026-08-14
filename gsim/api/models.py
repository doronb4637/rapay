"""
Request/response models -- and the GSim wrapper's stricter contract.

`core.ConnectionConfig.from_json` accepts a config whose `Structures` key is
absent (it is one of the free-form `extra` keys; `_import_config_libs` simply
no-ops). That is correct for core -- a byte-oriented deployment genuinely needs
no message layouts. It is wrong for GSim: without registered layouts the
Messages panel has nothing to list and the Inspector has no schema to render a
form from, so the connection would be created and then be useless.

So `Structures` is **mandatory here**, at the API edge, and core stays
unchanged. Same for per-peer `unitCode`, which core already requires -- stated
explicitly so the modal can show a field-level error instead of surfacing a
`ValueError` string from deep in `from_json`.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Protocol = Literal["tcp", "udp", "multicast", "dds"]
Side = Literal["client", "server", "publisher", "subscriber", "sender", "receiver"]

#: side values each protocol actually accepts, mirroring how the protocol
#: classes read `config.side` (multicast derives direction from it entirely).
_SIDES_BY_PROTOCOL: dict[str, set[str]] = {
    "tcp": {"client", "server"},
    "udp": {"client", "server"},
    "multicast": {"sender", "receiver", "client", "server"},
    "dds": {"publisher", "subscriber"},
}


class PeerSpec(BaseModel):
    """One entry of core's `connections` block: a logical peer."""
    model_config = ConfigDict(extra="allow")   # echo_opcode & friends pass through

    name: str = Field(min_length=1, description="Logical peer name; labels the Received log.")
    port: int = Field(ge=0, le=0xFFFF)
    unitCode: int = Field(ge=0, le=0xFF, description="THEIR unit code -- selects the parse layout.")


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Display name; labels the Sent log.")
    protocol: Protocol
    side: Side
    ip: str = Field(min_length=1)
    local_ip: str = "0.0.0.0"
    unitCode: int = Field(ge=0, le=0xFF, description="OUR unit code -- stamped into every header.")

    peers: list[PeerSpec] = Field(min_length=1)

    #: MANDATORY in GSim, optional in core -- see module docstring.
    structures: list[str] = Field(
        min_length=1,
        description="IRS structure modules to import, e.g. 'Test.test_messages'. "
                    "Required by GSim: without them the Messages panel is empty.",
    )

    echo_opcode: int | None = Field(default=None, ge=0, le=0xFFFF)
    echo_interval: float | None = Field(default=None, gt=0)
    echo_timeout: float | None = Field(default=None, gt=0)
    extra: dict[str, Any] = Field(default_factory=dict, description="Protocol extras: ttl, mode, ...")

    autostart: bool = True

    @field_validator("structures")
    @classmethod
    def _no_blank_structures(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("at least one IRS structures module is required")
        return cleaned

    @model_validator(mode="after")
    def _check_consistency(self) -> "ConnectionCreate":
        allowed = _SIDES_BY_PROTOCOL[self.protocol]
        if self.side not in allowed:
            raise ValueError(f"side {self.side!r} is not valid for {self.protocol}; use one of {sorted(allowed)}")

        names = [peer.name for peer in self.peers]
        if len(set(names)) != len(names):
            raise ValueError("peer names must be unique within a connection")

        codes = [peer.unitCode for peer in self.peers]
        if len(set(codes)) != len(codes):
            # Core raises this too, but only after the modal has closed.
            raise ValueError("peer unitCodes must be unique within a connection")

        if self.echo_timeout is not None and self.echo_interval is not None:
            if self.echo_timeout <= self.echo_interval:
                raise ValueError("echo_timeout must be greater than echo_interval")
        return self

    def to_core_config(self) -> dict[str, Any]:
        """Build the exact JSON dict `ConnectionManager.create()` expects.

        Passing a dict (never a config *name*) is deliberate:
        `ConnectionManager._load_config` routes a `str` through
        `tools.file_functions.read_unit_config`, which is currently an
        unimplemented stub in core -- so the dict path is the only working one,
        and it is also the one that lets GSim build configs from the UI.
        """
        config: dict[str, Any] = {
            "protocol": self.protocol,
            "side": self.side,
            "ip": self.ip,
            "local_ip": self.local_ip,
            "unitCode": self.unitCode,
            "connections": {
                peer.name: {
                    "port": peer.port,
                    "unitCode": peer.unitCode,
                    **peer.model_dump(exclude={"name", "port", "unitCode"}),
                }
                for peer in self.peers
            },
            "Structures": self.structures,
        }
        if self.echo_opcode is not None:
            config["echo_opcode"] = self.echo_opcode
        if self.echo_interval is not None:
            config["EchoInterval"] = self.echo_interval
        if self.echo_timeout is not None:
            config["EchoTimeout"] = self.echo_timeout
        config.update(self.extra)     # ttl / mode / idl_file / qos_file ...
        return config


class ConnectionUpdate(ConnectionCreate):
    """Same shape: 'Edit' rebuilds the connection from scratch, because
    `ConnectionConfig` is frozen and a live `Connection` caches state from it."""


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_name: str = Field(min_length=1, description="Which configured peer to send to.")
    op_code: int = Field(ge=0, le=0xFFFF)
    payload: dict[str, Any] = Field(default_factory=dict)
