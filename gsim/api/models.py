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

#: A connection's name IS its identifier -- every route addresses it as
#: `/api/connections/{connection_name}/...`. So it is restricted to characters
#: that survive a URL untouched: no spaces, no `/` (which would split the path
#: into extra segments and match no route at all), no `?`/`#`/`%` (which end the
#: path or start an escape). Without this the very thing naming buys -- typing
#: a readable URL by hand -- breaks on the first connection called "My Unit".
#: Uniqueness is enforced separately, by the runtime, since only it knows what
#: already exists.
CONNECTION_NAME_PATTERN = r"^[A-Za-z0-9._-]+$"
CONNECTION_NAME_HELP = (
    "letters, digits, dot, underscore and hyphen only -- the name is used "
    "directly in API paths"
)
#: Names that would sit in the same path position as a fixed endpoint. Only
#: `POST /api/connections/import` exists today and it is POST-only, so a
#: connection called "import" would not actually be shadowed -- but "why does
#: this one connection behave oddly" is a miserable thing to debug, and one
#: reserved word is cheaper than the explanation.
RESERVED_CONNECTION_NAMES = frozenset({"import"})

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
    model_config = ConfigDict(extra="allow")   # protocol-specific extras still pass through

    name: str = Field(min_length=1, description="Logical peer name; labels the Received log.")
    port: int = Field(ge=0, le=0xFFFF)
    unitCode: int = Field(ge=0, le=0xFF, description="THEIR unit code -- selects the parse layout.")
    #: THIS LINK's IRS modules. A structures file defines the messages between
    #: one specific pair of units, so it belongs to the peer, not the
    #: connection -- see core/connections/CLAUDE.md 2b.
    structures: list[str] | None = Field(
        default=None,
        description="IRS structure modules for this link, e.g. 'Test.test_messages'. "
                    "Required on every peer of a multi-peer non-multicast connection.",
    )
    #: Per-peer override of the connection-level echo settings below. Same
    #: three keys; core resolves whichever THIS unit sets, falling back to the
    #: connection-level value only for the keys the unit leaves unset
    #: (`EchoSettings.resolve`, core/connections/config.py) -- so a peer may
    #: override just the opcode and still inherit the shared interval/timeout.
    #: Only meaningful on tcp/udp; the modal hides these for multicast/dds,
    #: same as the connection-level ones.
    echo_opcode: int | None = Field(default=None, ge=0, le=0xFFFF)
    echo_interval: float | None = Field(default=None, gt=0)
    echo_timeout: float | None = Field(default=None, gt=0)


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=64, pattern=CONNECTION_NAME_PATTERN,
        description=f"Identifies this connection everywhere; {CONNECTION_NAME_HELP}.",
    )
    protocol: Protocol
    side: Side
    ip: str = Field(min_length=1)
    local_ip: str = "0.0.0.0"
    unitCode: int = Field(ge=0, le=0xFF, description="OUR unit code -- stamped into every header.")

    peers: list[PeerSpec] = Field(min_length=1)

    #: Connection-level fallback. Only meaningful when there is ONE link to
    #: apply it to (or multicast, one sender to many receivers over one IRS);
    #: `_check_consistency` enforces that, mirroring core's own rule. GSim still
    #: requires layouts SOMEWHERE -- without them the Messages panel is empty
    #: and the Inspector has no form to render (see module docstring).
    structures: list[str] = Field(
        default_factory=list,
        description="IRS structure modules for a single-peer or multicast connection. "
                    "With several peers, each declares its own instead.",
    )

    echo_opcode: int | None = Field(default=None, ge=0, le=0xFFFF)
    echo_interval: float | None = Field(default=None, gt=0)
    echo_timeout: float | None = Field(default=None, gt=0)
    extra: dict[str, Any] = Field(default_factory=dict, description="Protocol extras: ttl, mode, ...")

    autostart: bool = True

    @field_validator("structures")
    @classmethod
    def _clean_structures(cls, value: list[str]) -> list[str]:
        """Drop blanks (the modal always renders one empty row) but do not
        require anything here -- whether a list is needed at all depends on the
        peer count, which only `_check_consistency` can see."""
        return [item.strip() for item in value if item and item.strip()]

    @model_validator(mode="after")
    def _check_consistency(self) -> "ConnectionCreate":
        if self.name.lower() in RESERVED_CONNECTION_NAMES:
            raise ValueError(
                f"{self.name!r} is reserved; it collides with a fixed API endpoint "
                f"in the same path position")

        allowed = _SIDES_BY_PROTOCOL[self.protocol]
        if self.side not in allowed:
            raise ValueError(f"side {self.side!r} is not valid for {self.protocol}; use one of {sorted(allowed)}")

        # A structures file defines the IRS for ONE link, so it belongs to a
        # peer. Multicast is the exception core makes too: one sender fans out
        # to many receivers over a single shared IRS.
        fans_out = self.protocol == "multicast"
        for peer in self.peers:
            if peer.structures is not None:
                peer.structures = [s.strip() for s in peer.structures if s and s.strip()]

        if len(self.peers) > 1 and not fans_out:
            if self.structures:
                raise ValueError(
                    "connection-level 'structures' is only allowed when the connection has "
                    "exactly one connected unit, or is multicast; a structures file defines "
                    "ONE link, so declare each connected unit's own 'structures' instead")
            missing = [peer.name for peer in self.peers if not peer.structures]
            if missing:
                raise ValueError(
                    f"connected units {missing} have no 'structures'; every unit on a "
                    f"multi-unit {self.protocol} connection must declare its own")
        elif not self.structures and not any(peer.structures for peer in self.peers):
            # GSim is stricter than core here: with no layouts at all the
            # Messages panel is empty and the Inspector has no form to build.
            raise ValueError("at least one IRS structures module is required")

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

        for peer in self.peers:
            if peer.echo_timeout is not None and peer.echo_interval is not None:
                if peer.echo_timeout <= peer.echo_interval:
                    raise ValueError(
                        f"connected unit {peer.name!r}: echo_timeout must be greater than echo_interval")
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
                    # Canonical spelling, so core sees one key rather than any
                    # of the several it would also accept for the same thing --
                    # and matching the exact spelling the connection-level echo
                    # keys use below, so Save/Load round-trips identically.
                    **({"Structures": peer.structures} if peer.structures else {}),
                    **({"echo_opcode": peer.echo_opcode} if peer.echo_opcode is not None else {}),
                    **({"EchoInterval": peer.echo_interval} if peer.echo_interval is not None else {}),
                    **({"EchoTimeout": peer.echo_timeout} if peer.echo_timeout is not None else {}),
                    **peer.model_dump(exclude={
                        "name", "port", "unitCode", "structures",
                        "echo_opcode", "echo_interval", "echo_timeout",
                    }),
                }
                for peer in self.peers
            },
        }
        if self.structures:
            # Only when it is legal (single unit, or multicast) -- emitting it
            # alongside several units is a load-time ValueError in core.
            config["Structures"] = self.structures
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


class ConnectionImport(BaseModel):
    """One entry of a Save/Load session file, as re-submitted by `POST
    /api/connections/import`.

    Deliberately bypasses `ConnectionCreate`'s stricter contract: `config` is
    already core-shaped JSON, exactly what `GET /api/connections` returns as
    each record's `"config"` (i.e. whatever `ConnectionManager.create()` was
    originally given). Re-deriving `peers`/`structures` from it and
    re-validating through `ConnectionCreate` would lose any protocol-specific
    `extra` keys the frontend form doesn't model (idl_file, qos_file, ttl,
    mode, ...) -- `ConnectionCreate.extra` only carries what the modal
    explicitly collected when the connection was first created, not
    everything core's config actually holds. Importing the raw config makes
    Save/Load a lossless round trip instead of a second, weaker constructor.
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=64, pattern=CONNECTION_NAME_PATTERN,
        description=f"Identifies this connection everywhere; {CONNECTION_NAME_HELP}.",
    )
    config: dict[str, Any]
    autostart: bool = True


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_name: str = Field(min_length=1, description="Which configured peer to send to.")
    op_code: int = Field(ge=0, le=0xFFFF)
    payload: dict[str, Any] = Field(default_factory=dict)


class BehaviourRequest(BaseModel):
    """Configure (or reconfigure) the behaviour on one message route.

    There is at most one behaviour per `(connection, unit_name, op_code)`, so
    this is an upsert -- see `core_gateway/behaviours.py` for why the route is
    the key. `kind` is validated against the engine's own `KINDS` rather than a
    duplicated `Literal`, so adding a behaviour shape there does not require
    remembering to widen this too.
    """
    model_config = ConfigDict(extra="forbid")

    unit_name: str = Field(min_length=1, description="Which configured peer to send to.")
    op_code: int = Field(ge=0, le=0xFFFF)
    kind: str = Field(default="periodic", description="Behaviour shape, e.g. 'periodic'.")
    #: Seconds between sends. Only meaningful for `periodic`; kept optional so a
    #: future one-shot/burst kind need not send a meaningless value.
    interval: float = Field(default=1.0, gt=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
