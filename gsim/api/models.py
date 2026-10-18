"""
Request/response models -- and the GSim wrapper's stricter contract.

Core itself requires a `Structures` list for every socket link and checks each
peer's name and unit code against it when the connection is built.

`Structures` and per-peer `unitCode` are mandatory here too, at the API edge,
stated explicitly so the modal can show a field-level error instead of surfacing a
`ValueError` string from deep in `from_json`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The filter vocabulary is imported from the engine that enforces it, not
#: re-typed as a `Literal` here -- adding an operator there must not require
#: remembering to widen this too. Same reasoning as `BehaviourRequest.kind`.
from gsim.core_gateway import (
    ACTIONS as FILTER_ACTIONS,
    MODES as FILTER_MODES,
    OPERATORS as FILTER_OPERATORS,
    BEHAVIOUR_LEGACY_KINDS,
    BEHAVIOUR_MAX_DELAY_MS,
    BEHAVIOUR_MODES,
    BEHAVIOUR_TRIGGERS,
    dds_defaults,
)

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

Protocol = Literal["tcp", "udp", "multicast"]
Side = Literal["client", "server", "sender", "receiver"]

#: side values each protocol actually accepts, mirroring how the protocol
#: classes read `config.side` (multicast derives direction from it entirely).
#: DDS has no side: what a unit publishes and subscribes is its Interface's.
_SIDES_BY_PROTOCOL: dict[str, set[str]] = {
    "tcp": {"client", "server"},
    "udp": {"client", "server"},
    "multicast": {"sender", "receiver", "client", "server"},
}

#: An IRS opcode, or a DDS topic name -- core takes either as a route key,
#: depending on the connection's protocol (see `core_gateway/routekey.py`).
RouteKey = Union[Annotated[int, Field(ge=0, le=0xFFFF)], Annotated[str, Field(min_length=1)]]


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
    #: Only meaningful on tcp/udp; the modal hides these for multicast,
    #: same as the connection-level ones.
    echo_opcode: int | None = Field(default=None, ge=0, le=0xFFFF)
    echo_interval: float | None = Field(default=None, gt=0)
    echo_timeout: float | None = Field(default=None, gt=0)


class IrsConnectionCreate(BaseModel):
    """A framed (tcp/udp/multicast) connection, whose messages are IRS layouts."""
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
    def _check_consistency(self) -> "IrsConnectionCreate":
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


class DdsConnectionCreate(BaseModel):
    """A DDS node: ONE unit of a generated DDS Interface.

    Everything else core derives from the Interface -- our unit code, the peers
    and the topics -- so a DDS config names no side, address, peers or
    structures, and core refuses any of them (`config._refuse_non_dds_keys`).
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=64, pattern=CONNECTION_NAME_PATTERN,
        description=f"Identifies this connection everywhere; {CONNECTION_NAME_HELP}.",
    )
    protocol: Literal["dds"]
    dds_interface: str = Field(min_length=1, description="Path to the generated DDS Interface .py.")
    unit: str = Field(min_length=1, description="Which DdsUnit of the Interface this node is.")
    domain_id: int | None = Field(default=None, ge=0)
    qos_file: str | None = None
    qos_profile: str | None = None
    autostart: bool = True

    @field_validator("dds_interface", "unit", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("qos_file", "qos_profile", mode="before")
    @classmethod
    def _blank_is_default(cls, value: Any) -> Any:
        """A blank optional field means "core's default", not an empty string."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    def to_core_config(self) -> dict[str, Any]:
        """Only the keys core reads for DDS. `domain_id`/`qos_file` are left
        out when they equal core's defaults, so a saved session does not pin
        one install's absolute QoS path into every other machine it is loaded
        on."""
        defaults = dds_defaults()
        config: dict[str, Any] = {
            "protocol": "dds", "unit": self.unit, "dds_interface": self.dds_interface,
        }
        if self.domain_id is not None and self.domain_id != defaults["domain_id"]:
            config["domain_id"] = self.domain_id
        if self.qos_file and Path(self.qos_file).resolve() != Path(defaults["qos_file"]).resolve():
            config["qos_file"] = self.qos_file
        if self.qos_profile:
            config["qos_profile"] = self.qos_profile
        return config


#: One request body for both shapes, picked by `protocol`. 'Edit' uses the
#: same shape: it rebuilds the connection from scratch, because
#: `ConnectionConfig` is frozen and a live `Connection` caches state from it.
ConnectionCreate = Annotated[
    Union[IrsConnectionCreate, DdsConnectionCreate], Field(discriminator="protocol")]
ConnectionUpdate = ConnectionCreate


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
    op_code: RouteKey
    payload: dict[str, Any] = Field(default_factory=dict)


class BehaviourCondition(BaseModel):
    """An optional gate on the incoming message, for an `on_received` trigger.

    Exactly the shape a filter rule uses minus the action, because it is exactly
    the same question -- both end up as one `core_gateway.fieldpath.Condition`.
    """
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Dotted field path on the INCOMING message.")
    op: str = Field(default="==", description="One of == != < <= > >=")
    #: An enum decodes to its member NAME, so a condition against one carries a
    #: string ("ON"); a scalar carries a number. The route checks the value
    #: against the field's own kind, where the schema is resolvable.
    value: float | int | str | None = None

    @field_validator("op")
    @classmethod
    def _known_operator(cls, value: str) -> str:
        if value not in FILTER_OPERATORS:
            raise ValueError(f"must be one of {sorted(FILTER_OPERATORS)}")
        return value


class BehaviourMapping(BaseModel):
    """Copy one field from the incoming message into the outgoing one."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: `from` is a Python keyword, so the field is `source`/`target` internally
    #: and aliased on the wire -- the JSON stays readable as `{from, to}`.
    source: str = Field(alias="from", min_length=1,
                        description="Dotted path on the INCOMING message.")
    target: str = Field(alias="to", min_length=1,
                        description="Dotted path on the OUTGOING message.")


class BehaviourRequest(BaseModel):
    """Configure (or reconfigure) one behaviour.

    An upsert keyed by `(connection, unit_name, op_code, trigger,
    trigger_unit_name, trigger_op_code)` -- the outbound route plus what fires
    it. Two rules sending one message on two different stimuli are different
    behaviours; two always-on periodic schedules on one route are the same one,
    which is the collision the key exists to force (see
    `core_gateway/behaviours.py`).

    `trigger`/`mode` are validated against the engine's own tuples rather than
    duplicated `Literal`s, so adding a trigger there does not require
    remembering to widen this too.
    """
    model_config = ConfigDict(extra="forbid")

    unit_name: str = Field(min_length=1, description="Which configured peer to send to.")
    op_code: RouteKey
    #: The legacy spelling of (trigger, mode). Kept working, and it wins when
    #: given, so a caller written before triggers existed needs no migration.
    kind: str | None = Field(default=None, description="Legacy shape name, e.g. 'periodic'.")
    trigger: str = Field(default="immediate",
                         description="'immediate', 'on_connect' or 'on_received'.")
    mode: str = Field(default="periodic", description="'once' or 'periodic'.")
    #: Which peer's message fires this, and which message. `on_received` only.
    trigger_unit_name: str | None = None
    trigger_op_code: RouteKey | None = None
    condition: BehaviourCondition | None = None
    #: Response latency before the send (or before a periodic action's first
    #: tick). Milliseconds, because that is the unit a protocol timing budget is
    #: written in; the engine converts once.
    delay_ms: float = Field(default=0.0, ge=0, le=BEHAVIOUR_MAX_DELAY_MS)
    mappings: list[BehaviourMapping] = Field(default_factory=list)
    #: Seconds between sends. Only meaningful when `mode` is 'periodic'.
    interval: float = Field(default=1.0, gt=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("trigger")
    @classmethod
    def _known_trigger(cls, value: str) -> str:
        if value not in BEHAVIOUR_TRIGGERS:
            raise ValueError(f"must be one of {list(BEHAVIOUR_TRIGGERS)}")
        return value

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        if value not in BEHAVIOUR_MODES:
            raise ValueError(f"must be one of {list(BEHAVIOUR_MODES)}")
        return value

    @model_validator(mode="after")
    def _trigger_consistency(self) -> "BehaviourRequest":
        """Refuse a request that describes a rule that cannot exist, here rather
        than in the engine, so the modal gets a field-level reason."""
        trigger = BEHAVIOUR_LEGACY_KINDS[self.kind][0] if self.kind else self.trigger
        if trigger == "on_received" and self.trigger_op_code is None:
            raise ValueError("trigger 'on_received' needs trigger_op_code")
        if trigger != "on_received":
            if self.condition is not None:
                raise ValueError(
                    "a condition tests the incoming message, so it needs trigger 'on_received'")
            if self.mappings:
                raise ValueError(
                    "value forwarding reads the incoming message, so it needs "
                    "trigger 'on_received'")
        return self


class FilterRule(BaseModel):
    """One condition on a received message: keep it, or drop it.

    `path` is dotted into the shape `Message.to_dict()` produces, so it may
    descend through structs and into a bitfield's bits -- but never across an
    array, because a single path cannot name one element of 35. That is enforced
    against the real schema in `routes/filters.py`, which is the only layer that
    knows which message this rule is for.
    """
    model_config = ConfigDict(extra="forbid")

    action: str = Field(description="'keep' or 'drop'.")
    path: str = Field(min_length=1, description="Dotted field path, e.g. 'Header.Mode'.")
    op: str = Field(default="==", description="One of == != < <= > >=")
    #: An enum field decodes to its MEMBER NAME, so a rule against one carries a
    #: string ("ON"), not a number. A scalar carries a number. Both are allowed
    #: here; `routes/filters.py` checks the value against the field's own kind.
    value: float | int | str | None = None

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in FILTER_ACTIONS:
            raise ValueError(f"must be one of {list(FILTER_ACTIONS)}")
        return value

    @field_validator("op")
    @classmethod
    def _known_operator(cls, value: str) -> str:
        if value not in FILTER_OPERATORS:
            raise ValueError(f"must be one of {sorted(FILTER_OPERATORS)}")
        return value


class FilterRequest(BaseModel):
    """Configure (or reconfigure) the filter on one INBOUND message route.

    An upsert keyed by `(connection, unit_name, op_code)`, exactly like
    `BehaviourRequest` -- two filters on one route would contradict rather than
    compose. `unit_name` here is the SENDER's configured peer name, because a
    received message is decoded under the peer's unit code.
    """
    model_config = ConfigDict(extra="forbid")

    unit_name: str = Field(min_length=1, description="Which peer sends this message.")
    op_code: RouteKey
    mode: str = Field(default="all", description="'all', 'change', or 'field-change'.")
    change_field: str | None = Field(
        default=None, description="Field to watch, required when mode is 'field-change'.")
    rules: list[FilterRule] = Field(default_factory=list)
    armed: bool = True

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        if value not in FILTER_MODES:
            raise ValueError(f"must be one of {list(FILTER_MODES)}")
        return value

    @model_validator(mode="after")
    def _change_field_present(self) -> "FilterRequest":
        if self.mode == "field-change" and not self.change_field:
            raise ValueError("mode 'field-change' needs a change_field")
        return self
