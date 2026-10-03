# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`connection_framework`: a modular, JSON-configured connection management system for TCP, UDP,
Multicast and RTI Connext DDS, targeting Python 3.10. It's a library (imported as `connections`),
not an application.

## Commands

`core` is an ordinary Python package (`core/__init__.py`), rooted at the REPO ROOT — not a `sys.path`
entry in its own right. Every internal import is absolute through it: `core.connections`, `core.IRS`,
`core.tools`, `core.annotations`. So it's the repo root that needs to be on `sys.path`, not `core/`:

- Run the manual smoke-test harness (real loopback sockets, no pytest dependency), from anywhere —
  it resolves its own path (inserts the repo root, its own great-grandparent):
  `python core/connections/test_framework.py`
- Run the pytest suite (`core/tests/`) from the repo root — `pytest.ini` there sets
  `pythonpath = .`: `pytest`
- Sanity-check imports: `python -c "import core.connections; print('import ok')"` (run from the repo
  root, or set `PYTHONPATH` to it)
- Compile-check the whole package: `python -m compileall -q core/connections`

(On Windows PowerShell from somewhere other than the repo root, set `PYTHONPATH` first:
`$env:PYTHONPATH = "<repo-root>"`.)

There is no separate lint/build step. DDS support (`dds.py`) is optional and self-disables
(`DdsConnection = None`) when the RTI Connext Python API isn't installed; the rest of the
framework works without it.

## Layout

```
config.py      ConnectionConfig: JSON -> typed config, unit<->port mapping
framing.py     (UnitCode,OpCode,DataLength) little-endian struct pack/unpack
base.py        _EventLoopThread (sync/async bridge) + Connection ABC
               + subscribe-or-drop delivery + per-unit state, and the
               `Unit` Protocol both Connection and CompositeUnit satisfy
_routes.py     RouteTable -- who owns a (unit_code, key) route, and the
               rule that exactly one thing does (subscription XOR callback).
               The key is the opcode on framed links, the topic name on DDS
_echo.py       UnitEchoSupervisor -- per-unit heartbeat sender, liveness
               clock and timeout watchdog; armed on connect, disarmed on drop
tcp.py         TcpConnection
udp.py         UdpConnection
multicast.py   MulticastConnection (direction derived from config.side)
dds.py         DdsConnection (RTI Connext; native payloads, no framing, topic routing)
dds_config.py  DDS Interface loading/validation + one unit's view of it (DdsUnitConfig,
               TopicSpec), DEFAULT_DOMAIN_ID / DEFAULT_QOS_FILE. Imports no rti
composite.py   CompositeUnit -- combines direction-limited connections into one Unit
manager.py     ConnectionManager -- factory + centralized absolute-shutdown
```

### Sibling packages this one depends on

`connections` owns transports, routing and lifecycle; it deliberately owns
neither the payload codec nor the project's generic helpers:

| import | used for |
| --- | --- |
| `IRS.irs_parser.irs_to_bytes` / `parse_irs` | the payload codec, in `base.Connection._encode` / `_decode` |
| `tools.general.extract_opcode` / `validated_opcode` | every opcode entering the framework (`Connection._message_key`), and `config._as_opcode` |
| `tools.general.validated_unitcode` | both kinds of unit code, in `config._as_unit_code` |
| `tools.general.import_modules` | `Connection.__init__` -- imports every module in a config's `Structures` (connection-level plus per-unit) so `IRS.REGISTRY` is populated however the connection was built |
| `tools.general.resolve_module_name` | `config.resolve_structures` -- the namespace a structures spelling registers under. Shared with `import_modules` so the two can never disagree |
| `tools.file_functions.read_unit_config` | `ConnectionManager.create(name, "TcpServer")` -- loads `config/Units/TcpServer.json` |

The split of labour with `tools` is consistent: `tools` answers *what an
opcode/unit code is written as* (so `99` and `"0x63"` mean the same thing
everywhere in the project), this package answers *what it has to fit into*
(the uint16/uint8 header fields in `framing.py`). Coercion is theirs, range
checks stay here.

## Conventions

### Error handling

Fail loudly, but only where the fault is ours.

- **A message we receive that IRS doesn't define** is a third-party problem. Log a warning and
  move on -- never crash the read loop or drop the link over it.
- **Our own code subscribing to a message that doesn't exist** is our bug. Raise immediately, at
  the subscribing call, not later at delivery.

### Comments and docs

Concise. The reader is an expert Python developer: skip the *what*, and explain the *why* only
where the logic is genuinely non-obvious.

## Architecture

### 1. The sync/async boundary

Everything runs on **one** background thread that owns **one** asyncio event loop for the whole
process (`base._EventLoopThread`, a lazily-created singleton). All socket I/O happens as
coroutines on that loop; the public API (`Connection.start/close/send_message/receive_message`) is
plain, blocking, synchronous Python that marshals each call onto the loop thread with
`asyncio.run_coroutine_threadsafe(...).result(timeout=...)`. Callers never import `asyncio`.

### 2. Message framing

`framing.py` pre-compiles the header format once at import time (`struct.Struct("<BHH")`:
uint8 UnitCode, uint16 OpCode, uint16 DataLength). `MessageHeader` is a frozen, `slots=True`
dataclass. `pack_message`/`unpack_message` and `IRSDataError` round out the module.
`FramedConnection` (in `base.py`) mixes this into `TcpConnection`, `UdpConnection` and
`MulticastConnection`; `DdsConnection` never touches this module (native payloads instead).

### 2a. IRS payload codec

The header is this framework's business (TCP needs `DataLength` to find message boundaries);
everything after it belongs to the `IRS` package. `FramedConnection` sets `uses_irs_parser = True`,
so on those connections:

- `Connection._encode(opcode, message)` calls `irs_to_bytes(own_unit_code, opcode, message)` —
  **our** code, since the receiver needs to know who sent it. Used by `send_message` and
  `periodic_sending` (which encodes once, at schedule time). Raw `bytes` pass through untouched
  (this is also how the config-supplied echo payload travels).
- `Connection._decode(unit_code, opcode, payload)` calls `parse_irs(their_unit_code, ...)` — the
  **sender's** code selects the message layout. Called from `_dispatch_incoming` *after* a route
  owner is found, so unowned messages cost nothing.
- Both wrap the parser call in `try/except` and re-raise as `IRSDataError` naming the unit code
  and opcode. On **send** that propagates to the caller: handing `send_message` an object IRS
  can't encode is a programming error and fails loudly.
- On **receive** it does not. `IRS.irs_parser` is strict — an unregistered `(unitCode, opCode)`
  or a payload that doesn't fit its layout raises — so `_dispatch_incoming` catches it, logs it
  via `logger.exception` (full traceback: an unparseable message is a real problem), and
  **drops that message**. One bad message never costs the read loop, the connection, or the
  route's owner: a parked `receive_message()` stays parked and goes on waiting for a message it
  can actually return, up to its own `timeout`. A peer sending a malformed frame must not be able
  to fail a caller who asked for a good one, and a standing `handle_on_receive` callback simply
  is not invoked for it.
- `parse_irs` returning `None` likewise means "no conversion" and the raw bytes are delivered. A
  2-tuple result is unwrapped to its second element (`parse_irs` returns
  `(message_name, message_object)`).

Both codec calls are **scoped to the link** — see §2b. `_encode` takes the destination unit name
for exactly that reason, even though the name never reaches the wire.

`DdsConnection` leaves `uses_irs_parser = False`: its payloads are typed samples and both codec
hooks become no-ops. The eager check in `receive_message`/`handle_on_receive` goes through
`Connection._validate_route()` rather than calling `validate_irs` directly, precisely so DDS can
answer the same question ("could this connection ever deliver this route?") against its topic
list. Overriding it is required, skipping it is not an option — an unanswerable question is how
a `receive_message` that blocks forever gets written.

### 2b. Structures are per-LINK, and layouts are namespaced by their module

A structures file describes **one link** — one specific server to one specific client, usually both
directions (`IRS/Structures/Tiful/tiful_to_dtu.py` registers unit `0x01` *and* `0x02`). Multicast is
the sole exception: one sender fans out to many receivers over a single shared IRS.

That matters because a process talking to two peers loads two structures files, and both register
layouts under **our own** unit code — which is identical for every peer. Keyed by unit code alone,
the second import silently erased the first wherever the two shared an opcode, and `_encode` had no
way to tell the links apart even in principle. So `IRS.REGISTRY` keys by namespace first:

```python
STRUCTURE_REGISTRY: dict[Namespace, dict[UnitCode, dict[OpCode, IrsMessage]]]
PAIR_REGISTRY:      dict[Namespace, dict[UnitCode, UnitCode]]
```

The namespace is the structures module's `__name__`, captured from the calling frame by
`register_message`, so **no structures file needed a single edit** — importing one *is* the
namespaced registration.

Configs name the modules per unit:

```json
"unitCode": 22,
"connections": {
  "RadarUnit":   {"port": 2000, "unitCode": 7, "Structures": ["Radar.radar_link"]},
  "TrackerUnit": {"port": 2001, "unitCode": 8, "Structures": ["Tracker.tracker_link"]}
}
```

`ConnectionConfig` resolves each unit's list at load time (`resolve_structures`, the same
connection-level-default/per-unit-override shape as `EchoSettings.resolve`, and resolving as a
**group** for the same reason) and stores it on `UnitEndpoint.structures`. `Connection` caches the
mapping and reads it through `_structures_for(unit_name)`, mirroring `_echo_for`. Every IRS call
takes that scope: `irs_to_bytes`, `parse_irs`, and the eager `validate_irs` in `receive_message` /
`handle_on_receive`.

Three rules worth stating plainly:

- **A connection-level `Structures` is only legal with exactly one configured unit**, or on
  multicast. With several units it would scope all of them to one namespace, which is the bug
  itself; `from_json` rejects it and says where to move the lists.
- **An empty scope means unscoped**, not "no layouts": every registered module is searched. That is
  what a byte-oriented unit gets, and what every config written before this existed keeps getting.
- **An unscoped lookup that matches two different modules raises `IRSAmbiguousError`** naming both,
  rather than picking the last import. It is deliberately *not* a subclass of `IRSNotFoundError` —
  `is_irs_exist` swallows that one, and an ambiguous route reported as absent is the original silent
  bug all over again.

`PAIR_REGISTRY` is a **whole-unit** alias, namespaced the same way: a file written for the 1↔2 link
can serve 1↔14 with `register_pair(2, 14)` (second argument is the alias), and a unit that has its
own layouts is never redirected. Namespacing it matters — two files aliasing one code to different
canonical units is the same collision in a different dress.

`resolve_module_name` in `tools.general` is the single source of the namespace, used by both
`ConnectionConfig` (before the import) and `import_modules` (during it), which is what stops a link
being scoped to a namespace nothing ever registered under. It also gives a `.py` path *inside*
`IRS/Structures` its ordinary dotted name, so picking a file through GSim's browser and typing its
dotted spelling are one namespace, not two.

### 3. Unit routing: ours vs theirs

Two different unit codes, and the distinction is load-bearing:

```json
"unitCode": 3,
"connections": {
  "RadarUnit":   {"port": 5000, "unitCode": 7},
  "TrackerUnit": {"port": 5001, "unitCode": 8}
}
```

- **Top-level `unitCode` is OUR code** — `config.unit_code`, cached as `Connection._own_unit_code`.
  **Required** (`ValueError` from `from_json` if missing), uint8-checked. It is what `_frame()`
  stamps into every outgoing header and what `irs_to_bytes` is called with, so the peer can tell
  who sent the message.
- **`connections[name].unitCode` is THEIR code** — the remote unit's identity. It keys
  `_subscriptions`/`_callbacks`/`_periodic_tasks` and is what `parse_irs` is called with when
  decoding what that unit sent.

Inbound routing still comes from the transport (which socket/port a message arrived on), not from
the header, so the header's unit code is informational to us and identifying to the peer.

It's **required** — `ConnectionConfig.from_json` raises `ValueError` if missing/empty, no
default/anonymous-unit fallback. Each connection's `unitCode` is likewise **required** (no
default/derived value -- a spec missing it is a load-time `ValueError`), and always range- and
collision-checked; ports must be unique too. Lookups: `config.connections[name]` (the
`UnitEndpoint`), `unit_names`, `unit_codes`, `unit_for_code(code)`.
`ConnectionConfig` is `frozen=True, slots=True` — a live `Connection`
caches state derived from it, so mutating it post-construction would desync those caches.

### 4. opcode: mandatory on send, subscription key on receive

```python
connection.send_message(data: IrsMessage | bytes, opcode: int, unit_name: str | None = None) -> None
connection.receive_message(opcode: int, unit_name: str | None = None,
                            timeout: float | int | None = None,
                            trigger_function: Callable[[], Any] | None = None) -> IrsMessage
connection.handle_on_receive(opcode: int, callback_func: Callable[[bytes], Any],
                             unit_name: str | None = None) -> None
connection.stop_on_receive(opcode: int, unit_name: str | None = None) -> bool
```

`opcode` is mandatory on send. `unit_name` is optional only when exactly one unit is connected
(auto-resolved); otherwise required and validated — `receive_message()` only returns a message
matching both the requested `opcode` and resolved `unit_name`.

The parameter is a *selector*, turned into the route key by `Connection` hooks:
`_message_key(selector)` (default `tools.general.extract_opcode`; `None` is a `TypeError`) and
`_send_key(data, selector)` (default: the same, so framed links require the opcode). Sends pick
their destination through `_send_unit(unit_name, key)`. **DDS overrides all three**: its selector
is the topic — the `@idl.struct` class, a sample of it, or the topic name — its key is the topic
name; on send the selector is optional (a sample names its own topic, and a selector naming a
different topic is refused), and the destination is None (every subscriber) unless the caller
names a subscriber of the topic. DDS also overrides `_resolve_route` so a receive's `unit_name` may
be omitted whenever exactly one peer is at the other end of the topic.

### 5. Subscribe-or-drop message filtering

The system does **not** buffer incoming messages indefinitely. `Connection` keeps
`self._subscriptions: dict[(unit_code, opcode), asyncio.Future]`. Calling `receive_message()`
registers a future under that key and blocks until a matching message arrives or timeout fires.
Keyed by numeric `unit_code` (what's actually on the wire), not name. One future per route — a
second concurrent `receive_message()` for the same route raises `RuntimeError`.

Every subclass's read loop routes through `self._dispatch_incoming(unit_name, opcode, payload)`,
which: (1) consumes echoes first, then (2) hands the message to whoever owns that route — a
parked `receive_message()` future, else a standing `handle_on_receive()` callback. **If neither
exists, the message is discarded immediately.**

- **`trigger_function`** closes the request/response race: `receive_message` arms the
  subscription *before* running the trigger, so a reply that arrives immediately after sending
  is never dropped:
  ```python
  unit, reply = asker.receive_message(
      REPLY_OPCODE, timeout=3,
      trigger_function=lambda: asker.send_message(b"ping", REQUEST_OPCODE),
  )
  ```
  The trigger runs on the caller's thread; if it raises, the subscription is released and the
  exception propagates.

- **`handle_on_receive`** registers a standing `callback_func(payload)` for a route until
  `stop_on_receive()`. Registrations survive `close()`, so a restarted connection keeps them. Callbacks run on an **executor thread, never the event loop**
  (a callback that called back into the sync API from the loop thread would deadlock).
  Exceptions inside callbacks are logged, not propagated.

- A route is either polled or handled, **never both** — mixing raises `RuntimeError`.

### 5b. Class-based handlers (`UnitHandler`)

`handlers.py` adds a declarative alternative to calling `handle_on_receive()` by hand for every
opcode a unit answers: subclass `UnitHandler`, set `unitCode` to the *peer's* configured code
(the "theirs" code from section 3 above, not this process's own), and tag each handling method
with `@route(opCode=...)`:

```python
class TestHandler(UnitHandler):
    unitCode = 0x01

    @route(opCode=0xFFFF)
    def handle_message(self, message):
        self.unitConnection.send_message(reply, REPLY_OPCODE)

manager.create("unit_name", config_data, handler_class=TestHandler)
```

`@route` is a pure marker (tags the function with the opcode); `UnitHandler.__init_subclass__`
does the real work, once, at class-DEFINITION time: it walks the class's MRO for every tagged
method and builds `cls._routes: dict[opcode, method_name]` — so a subclass overriding a route
method (and re-tagging it) replaces the route, and dropping the tag silently un-routes it. Two
methods claiming the same opcode is a `TypeError` right there, before any config is involved.

`ConnectionManager.create(..., handler_class=...)` / `create_composite(..., handler_class=...)`
install the handler right after the `Connection`/`CompositeUnit` is built, **before** it's
registered with the manager — so a bad `unitCode` (no configured unit carries it) or a bad route
(an opcode `IRS.REGISTRY` doesn't know) fails `create()` atomically, same as every other load-time
config error in this package.

**The load-bearing design point: installing a class handler does nothing but call
`unit.handle_on_receive(opcode, bound_method, unit_name=...)` once per route** — an ORDINARY
`_callbacks` entry, indistinguishable from one registered by hand. No new dispatch tier exists in
`_dispatch_incoming`, and none was needed: a class-routed opcode inherits every rule
`handle_on_receive` already has for free — mutual exclusion with a live `receive_message()` on the
same route (section 5), executor-thread execution (so a route method may call
`self.unitConnection.send_message(...)` synchronously, no `async`/`await` — this package's public
API stays synchronous end to end, see section 1), eager `validate_irs()` at registration, and
exceptions logged and swallowed rather than killing the read loop.

### 5a. Per-unit connection state

`Connection` tracks which units currently have a live peer in `self._active_units` (distinct from
`config.unit_names`, which is only what was *configured*). Protocol classes report
transitions on the loop thread:

| protocol | connected when | disconnected when |
| --- | --- | --- |
| TCP server | `_on_client` accepts a peer (a previous one is closed) | that peer's read loop ends |
| TCP client | `open_connection` succeeds (again, after a drop, with `start(retry=True)`) | its read loop ends |
| UDP client | `_do_start` (remote_addr known), then any inbound datagram | unit disconnect |
| UDP server | any inbound datagram (`_remember_peer`) | unit disconnect (learned peer forgotten) |
| Multicast | `_do_start`, then any inbound datagram | unit disconnect |
| DDS | `_do_start` (entities built) | unit disconnect |

DDS is the one row with no per-unit transport behind it: reader/writer belong to a *topic* and
serve every unit speaking it, so `_do_disconnect_unit` closes nothing. Nothing triggers it
either — echo is rejected on DDS configs, so the watchdog never arms.

`_mark_unit_connected` / `_mark_unit_disconnected` are idempotent and are the **only** things that
arm or disarm a unit's echo; `_mark_unit_connected` is also the single trigger for an on-connect
callback (section 5c). Every transition fires `_notify_state_change()`, which *swaps in* a
fresh `asyncio.Event` rather than clearing the old one, so concurrent waiters can't consume each
other's wake-up.

```python
connection.wait_for_connected_units(target: int | str | list[str],
                                    timeout: float | int | None = None) -> bool
connection.active_units  # -> set[str] snapshot
```

Blocks the calling thread until the target is met; returns `True` when met, `False` on timeout.
Unknown unit names and counts exceeding what's configured raise `ValueError` in the caller's
thread rather than becoming a wait that could never succeed. `CompositeUnit` waits on each member
in turn against one shared deadline.

### 5c. Sending on connect (`handle_on_connect`)

The connect-time counterpart to `handle_on_receive` (section 5): a standing callback invoked the
moment a unit gains a usable peer, so a handler can greet it (`send_message`) without polling
`active_units`. No opcode — a connect event isn't a message — so it's keyed by unit name alone in
`self._connect_callbacks: dict[unit_name, callback]`, mutated through the loop thread the same way
`_callbacks` is, and fired from `_mark_unit_connected` (section 5a) the same tick the unit's echo is
armed.

```python
connection.handle_on_connect(callback_func: Callable[[str], Any], unit_name: str | None = None) -> None
connection.stop_on_connect(unit_name: str | None = None) -> bool
```

Runs on an executor thread, never the event loop — same reasoning as an on-receive callback: it may
call back into this connection's sync API, and `_mark_unit_connected` always runs ON the loop thread,
so running the callback inline there would deadlock the first `send_message` inside it. A unit
already carrying a callback must be released with `stop_on_connect()` first, the same exclusive-slot
rule `handle_on_receive` enforces per route. Registering it on a unit that is *already* connected
does not fire retroactively — only the next transition into connected does — which is a non-issue for
the normal ordering (`install_handler` runs before `start()`, so nothing is ever missed) and is
called out explicitly for the one case it matters: registering by hand on a connection already
running.

**Declarative form**: `handlers.py`'s `@on_connect` tags one `UnitHandler` method — no opcode,
since a handler already answers for exactly one peer, so there's only one connect event to tag.
`__init_subclass__` collects it the same way it collects `@route` methods, and rejects two
`@on_connect`-tagged methods on one class with a `TypeError` at class-definition time. Installing it
is, like section 5b's routes, nothing but an ordinary `unit.handle_on_connect(bound_method,
unit_name=...)` call — no new dispatch tier. `CompositeUnit.handle_on_connect` forwards to the
receive-capable member, matching `_config_unit_codes`'s existing choice of that member as canonical
for resolving a composite's unit names in `install_handler`.

### 6. The echo lifecycle

Parsed by `config.EchoSettings`:

| key | meaning | default |
| --- | --- | --- |
| `echo_opcode` | one opcode for both directions | -- |
| `recv_echo_opcode` / `send_echo_opcode` | distinct inbound/outbound opcodes | -- |
| `EchoInterval` (`echo_interval`) | seconds between outbound echoes | `1.0` |
| `EchoTimeout` (`echo_timeout`) | seconds of silence before the unit is dropped | `5.0` |

Stays inactive for a unit unless both of that unit's opcodes resolve. The heartbeat body is
always empty (`b""`). `EchoTimeout` must exceed
`EchoInterval` (config-time `ValueError` otherwise, naming the unit when it came from a unit block).

#### Hierarchical resolution: connection-level default, per-unit override

Every key above is accepted at **both** levels — in `extra` (connection-wide) and inside an
individual unit's dict in `connections`:

```json
"unitCode": 3,
"connections": {
  "RadarUnit":   {"port": 5000, "unitCode": 7, "echo_opcode": 10},
  "TrackerUnit": {"port": 5001, "unitCode": 8},
  "SilentUnit":  {"port": 5002, "unitCode": 9, "echo_opcode": null}
},
"echo_opcode": 99,
"EchoInterval": 1.0
```

RadarUnit heartbeats on 10, TrackerUnit falls back to 99, both at the shared 1.0s interval.
Echo is a property of a *link*, not of a process, so one connection may legitimately be talking to
peers that heartbeat differently — or to one that heartbeats and one that doesn't.

`EchoSettings.resolve(unit_spec, extra)` merges the two at **two granularities**, and the
difference is load-bearing:

- The three **opcode** keys resolve as a **group**. A unit naming any of them is describing its
  whole heartbeat, so the global opcodes drop out entirely rather than half-applying — a unit with
  `{"echo_opcode": 10}` under a global `{"recv_echo_opcode": 99}` must not end up receiving on 99
  and sending on 10, a link neither peer configured.
- `EchoInterval` / `EchoTimeout` resolve **individually**: a unit overriding only
  its timeout still wants the shared interval, and `timeout > interval` is checked on the merge.

Missing at both levels means echo stays off **for that unit alone** — the same "absent means
disabled" rule as before, now applied per unit instead of per connection. An explicit `null` on
any opcode key (SilentUnit above) is the opt-out: a present key names the group even when null,
so the connection-level opcodes drop out whatever spelling they used.

Resolution happens once, in `ConnectionConfig.from_json`, and lands in `UnitEndpoint.echo`
(`config.echo_for(unit)` / `config.unit_echoes`). Doing it at load time keeps the invariant the
rest of the framework leans on: a malformed echo key is a load-time `ValueError`, never a
heartbeat that silently never starts. `Connection` caches the mapping as `self._unit_echo` and
reads it through `self._echo_for(unit_name)` — on the connect path and in `_dispatch_incoming`,
which is why an opcode that is a heartbeat on one unit stays an ordinary application message on
another. `self._echo` remains the connection-level block, used as the fallback for a unit the
config never mentioned.

Three per-unit pieces, armed **per unit when that unit connects** — never by `start()`, which
would aim heartbeats at peers that don't exist yet — each on that unit's own resolved settings:

1. **Periodic sender** — every `EchoInterval` for as long as the unit stays connected; only on
   `can_send` connections. Re-checks `_active_units` each tick.
2. **Consumption** — `_dispatch_incoming` intercepts `recv_echo_opcode` messages before the
   subscribe-or-drop check: refreshes liveness, never visible to `receive_message()`, no reply
   sent (replying would double-answer with a single shared `echo_opcode`).
3. **Watchdog** — if no echo within `EchoTimeout`, disconnects just that unit
   (`_disconnect_unit`): cancels its periodic senders, fails any parked `receive_message()` with
   `ConnectionError`, drops its on-receive callbacks, and calls `_do_disconnect_unit` -- TCP closes
   the peer socket; UDP and Multicast keep theirs open so the unit's next datagram re-arms it.
   Other units on the same connection are untouched. Only on
   `can_receive` connections. Sleeps until each unit's deadline (`last echo + EchoTimeout`)
   rather than polling a fixed tick, so worst-case detection is exactly `EchoTimeout`, not ~2x.

A unit that drops has its echo tasks cancelled and its liveness entry cleared; a peer that comes
back re-arms both through the same `_mark_unit_connected` path, so reconnection needs no special
case. `_last_echo_at` is seeded at connect time, not at `start()`.

An echo send that raises `ConnectionError` retires the unit on the spot, through the same
`_disconnect_unit` the watchdog uses, rather than warning and retrying: the link is provably gone.
Other exceptions keep the old behaviour (log, retry next tick, let the watchdog decide).

### 6a. Periodic sending

```python
connection.periodic_sending(data: IrsMessage | dict, interval: int | float, opcode: int | None = None,
                            unit_name: str | None = None) -> None
connection.stop_periodic(opcode: int, unit_name: str | None = None) -> bool
```

Tracked in `self._periodic_tasks`, keyed by the same `(unit_code, opcode)` route as
subscriptions. Calling it twice for one route **replaces** the sender (old task cancelled and
awaited first — no doubled rate). A failed send is logged and retried next tick. Forwarded by
`CompositeUnit` to its send-capable member.

### 7. CompositeUnit

`composite.py` combines several direction-limited connections into one logical Unit via
composition (not monkey-patching). Every `Connection` exposes `can_send`/`can_receive`;
`MulticastConnection` derives these entirely from `config.side` (`Side.SENDER` -> send-only,
`Side.RECEIVER` -> receive-only, else duplex — no `mode`/`duplex` extra key for multicast).
`CompositeUnit.__init__` picks the one send-capable and one receive-capable member and raises
immediately if ambiguous/impossible; `send_message`/`receive_message` delegate to the right
member with the same signature as a plain `Connection`. `create_composite` registers only the
composite -- its members belong to it -- and registers nothing if any step fails.
`wait_for_connected_units` applies a name only to the members that configure it.

```python
beacon = mgr.create_composite("BeaconUnit", {
    "transport": {"protocol": "multicast", "side": "sender", ...},
    "receive":   {"protocol": "udp", "side": "server", "mode": "receive_only", ...},
})
```

> `test_framework.py`'s composite demo uses two directional **UDP** links in place of multicast
> because the sandbox's network namespace has no multicast routing. `multicast.py` is a complete
> implementation — swap `"protocol"` to `"multicast"` and `"side"` to `"sender"`/`"receiver"` on a
> network that supports it.

### 8. Lifecycle: absolute teardown

`Connection.close()` closes every socket/transport (`_do_stop()`), cancels and awaits every
tracked background task, then fails any `receive_message()` still parked with `ConnectionError`;
a `wait_for_connected_units()` parked across it returns False. While closing, `_closing` stops
dispatch, connect transitions and reconnects. A `_do_start` that fails part-way is undone the same
way (`_abort_start`: `_do_stop` plus cancelling the tasks that attempt spawned), so neither a
retry nor a later `start()` inherits half a connection. Every async task the framework starts (read loops, echo replies/senders/watchdogs,
`periodic_sending` schedules) goes through `self._track()`, so one sweep over `self._tasks`
covers all of them. `ConnectionManager.shutdown_all()` does this for every managed connection in
reverse creation order, tolerating individual failures.

One real Python <=3.12 bug is worked around in `tcp.py`: `asyncio.Server.wait_closed()` blocks
until every accepted connection has *also* finished, so peer writers must be closed **before**
awaiting it.

Two more teardown details, both about *normal* events that used to read as failures:

- `TcpConnection._read_loop` catches `OSError` separately from the final `except Exception`. A
  peer that vanishes instead of closing politely (RST — `WinError 64`/`10054`, killed process,
  interface down) is logged at INFO like a graceful close, not as a traceback. `logger.exception`
  is reserved for things that genuinely shouldn't happen.
- `_EventLoopThread` installs a loop exception handler (`_handle_loop_exception`) that demotes one
  specific artifact to DEBUG: an `OSError` with a Windows teardown code raised from asyncio's own
  `_ProactorBasePipeTransport._call_connection_lost`. CPython guards that best-effort
  `sock.shutdown()` for `ConnectionResetError` only, so an RST makes it report an unhandled error
  after the transport is already dead. The match is narrow (that callback, `OSError`, four codes);
  everything else goes to `loop.default_exception_handler` untouched.

### 9. DDS: one unit of a DDS Interface

A DDS node is configured as **one unit of a DDS Interface** — a generated Python module that is the
system contract — and its JSON names nothing else it must:

```json
{"protocol": "dds", "unit": "SensorUnit", "dds_interface": "C:/ICD/generated/dds_interface.py"}
```

The Interface is written in `core.DDS.interface`'s vocabulary (reference output for the user's
XSLT: `core/DDS/Interfaces/Example/example_interface.py`):

```python
from core.DDS import DdsUnit
from my_icd.topics import Status, Track      # ABSOLUTE imports -- see "class identity" below
INTERFACE_FORMAT = 1
SensorUnit = DdsUnit(unitCode=0x01, publish=(Track,), subscribe=(Status,))
ControlUnit = DdsUnit(unitCode=0x02, publish=(Status,), subscribe=(Track,))
```

Two naming rules, both load-bearing: **a unit's name is its variable**, and **a topic's name is its
class's `__name__`** — there is no topic table. `dds_config.resolve_unit` (called from
`ConnectionConfig._from_dds_json`, i.e. at load) validates the WHOLE Interface and derives this
unit's code, its peers (units publishing what it subscribes or subscribing what it publishes) and
its `TopicSpec`s, each carrying the peer `publishers`/`subscribers` the routing below leans on.
`config.dds` holds the result; `unitCode`/`connections` are derived from it (a peer's `port` is
the domain id), and `side`/`ip`/`local_ip` are **None**. A DDS config naming `side`, `ip`,
`local_ip`, `unitCode`, `connections`, `topics`, `idl_modules`/`idl_file`, `Structures` or an echo
key is refused with the reason, and so is any other key it does not read (including unknown
`header` sub-keys) — never silently ignored.

**Deployment defaults are constants** in `dds_config`: `DEFAULT_DOMAIN_ID` and `DEFAULT_QOS_FILE`
(absolute, derived from `__file__`, so it does not depend on the working directory). The
`domain_id`, `qos_file` and `qos_profile` keys override them; `qos_profile` defaults to the file's
`is_default_qos` profile. A missing QoS file fails at load, an unknown profile at construction.

**No opcodes.** DDS puts the topic on the wire, so DDS route keys are `(unit_code, topic_name)` and
callers select a topic by class, sample or name (§4). `RouteTable` and the echo check only hash and
compare keys, so the generalisation cost the framed protocols nothing. An int selector on DDS is a
`TypeError` saying so.

**A topic is not a unit.** A DataReader serves every publisher of its topic, so the sender is read
off the SAMPLE (`header.source_unit`, names configurable via `config["header"]`), falling back to
the Interface's sole publisher of that topic. A subscribed topic with several publishers — or one
the unit also publishes, since a participant hears its own writes — **must** carry the header;
`_check_senders_identifiable` makes a type that does not a construction error rather than silent
drops. A sample from a peer the Interface does not list as publishing the topic is a third-party
fault: warned about once, dropped. `_validate_route` / `_do_send` check the Interface the same way
on our side, where a mismatch is our bug and raises.

**Class identity.** The Interface imports the topic classes and application code builds samples
from them, so both must hold the SAME class objects — the IRS `_alias.py` problem in another form.
Hence: absolute imports in generated files (a path-loaded Interface has no package to be relative
to, and inventing one would load a private copy of every class — `load_dds_interface` refuses
relative imports and says why); path loads keyed in `sys.modules` by a digest of the *resolved*
path, so one file is one module; and a class named like a topic but not identical to it raises
"two copies of one generated module" instead of "unknown topic".

**QoS answers *how*.** One universal XML, per-topic settings inside profiles as `topic_filter`
attributes. `_qos_for(entity, topic)` uses `set_topic_*_qos(profile, topic)` for a named profile and
`get_topic_*_qos(topic)` for the default one — both evaluate filters. The profile-only accessors
(`datawriter_qos_from_profile`, `.datawriter_qos`) take no topic and silently hand every topic the
baseline; `dds.py` does not use them for entities. QoS applies at **construction**; RxO mismatch
fails silently — no error, no data.

**Lifecycle is RTI's**, with one ordering that is ours: `_close_entities` cancels the read loops
*first* (each has a ReadCondition on `rti.asyncio`'s process-wide WaitSet; cancelling is what
detaches it), then one `participant.close()` closes every contained topic, writer and reader. A
`_do_start` that fails part-way runs the same teardown before re-raising.

Smaller things that are each load-bearing:

- **`import rti.asyncio` is not dead code.** `DataReader.take_data_async` does not exist until that
  import monkey-patches it on. Its dispatcher is a process-global, first touched inside `_read_loop`
  (on the shared loop thread, where it must be), and `rti.asyncio.close()` is called only by the
  last `DdsConnection` to stop — `_live_connections` counts the ones that started successfully.
- **Self-reception** is filtered by `source_unit == own code`, preferred to `ignore_participant`,
  which would also block a legitimate second process of ours on the same host.
- **`can_send`/`can_receive` are the union of the topic directions**, so a subscribe-only unit
  can't be chosen as a `CompositeUnit` sender.
- **`_do_send` takes a typed sample** and stamps `source_unit`/`destination_unit` outbound, never
  overwriting values the caller set. `destination_unit` is informational: every subscriber still
  receives every sample.

Tests live in `core/tests/test_dds.py`. Everything that needs only `rti.connextdds` (Interface
loading, QoS parsing, selectors, routing via `_dispatch_incoming`, sends into a recording writer,
teardown ordering against a fake participant) runs anywhere it is installed; the one test that
puts real participants on a domain is gated behind `requires_license`, because creating a
`DomainParticipant` needs an RTI license and an environment without one must not read as a code
failure.
