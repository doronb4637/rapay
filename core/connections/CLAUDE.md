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

> **Known blocker with the shipped `tools` stub.** `tools.general.validated_opCode` /
> `validated_unitCode` are typed `int | str` but their bodies are `int(x, 0)`, which raises
> `TypeError` on an `int`. Every config in this repo writes unit codes and opcodes as ints, so
> with the stub in place `ConnectionConfig.from_json` rejects all of them and the suite fails at
> the first test. This is a `tools` implementation gap, not a `connections` one: the whole suite
> passes once those two functions accept ints (`int(x, 0) if isinstance(x, str) else int(x)`).
> Do not work around it inside this package — coercing to `str` at the call sites would defeat
> the point of routing coercion through `tools` at all.

## Layout

```
config.py      ConnectionConfig: JSON -> typed config, unit<->port mapping
framing.py     (UnitCode,OpCode,DataLength) little-endian struct pack/unpack
base.py        _EventLoopThread (sync/async bridge) + Connection ABC
               + subscribe-or-drop delivery + per-unit state + echo handling
tcp.py         TcpConnection
udp.py         UdpConnection
multicast.py   MulticastConnection (direction derived from config.side)
dds.py         DdsConnection (RTI Connext; native payloads, no framing)
composite.py   CompositeUnit -- combines direction-limited connections into one Unit
manager.py     ConnectionManager -- factory + centralized absolute-shutdown
```

### Sibling packages this one depends on

`connections` owns transports, routing and lifecycle; it deliberately owns
neither the payload codec nor the project's generic helpers:

| import | used for |
| --- | --- |
| `IRS.irs_parser.irs_to_bytes` / `parse_irs` | the payload codec, in `base.Connection._encode` / `_decode` (there is no local `irs_parser.py` any more; `connections/__init__.py` re-exports both names so `from connections import parse_irs` still works) |
| `tools.general.validated_opCode` | every opcode entering the framework: `send_message`, `periodic_sending`, `stop_periodic`, and `config._as_opcode` |
| `tools.general.validated_unitCode` | both kinds of unit code, in `config._as_unit_code` |
| `tools.general.import_modules` | `ConnectionManager._import_config_libs` -- imports every module in a config's `Structures` (connection-level plus per-unit) so `IRS.REGISTRY` is populated before the connection exists |
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
coroutines on that loop; the public API (`Connection.start/stop/send_message/receive_message`) is
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
  via `logger.exception` (full traceback: an unparseable message is a real problem), and then
  **delivers the raw payload** and carries on. One bad message never costs the read loop or the
  connection. This is also what keeps byte-oriented units working: they register no layouts at
  all, so every message they receive takes this path by design.
- `parse_irs` returning `None` likewise means "no conversion" and the raw bytes are delivered. A
  2-tuple result is unwrapped to its second element (`parse_irs` returns
  `(message_name, message_object)`).

Both codec calls are **scoped to the link** — see §2b. `_encode` takes the destination unit name
for exactly that reason, even though the name never reaches the wire.

`DdsConnection` leaves `uses_irs_parser = False`: its payloads are typed samples and both codec
hooks become no-ops.

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
default/anonymous-unit fallback. `config.ports` is derived, not separately maintained.
Each connection's `unitCode` is likewise **required** (no default/derived value -- a spec
missing it is a load-time `ValueError`), and always range- and collision-checked. Lookups:
`endpoint_for(name)`, `unit_code_for(name)`, `unit_from_port(port)`,
`port_for_unit(name)`. `ConnectionConfig` is `frozen=True, slots=True` — a live `Connection`
caches state derived from it, so mutating it post-construction would desync those caches.

### 4. opcode: mandatory on send, subscription key on receive

```python
connection.send_message(data: bytes, opcode: int, unit_name: str | None = None) -> None
connection.receive_message(opcode: int, unit_name: str | None = None,
                            timeout: float | int | None = None,
                            trigger_function: Callable[[], Any] | None = None) -> tuple[str, bytes]
connection.handle_on_receive(opcode: int, callback_func: Callable[[bytes], Any],
                             unit_name: str | None = None) -> None
connection.stop_on_receive(opcode: int, unit_name: str | None = None) -> bool
```

`opcode` is mandatory on send. `unit_name` is optional only when exactly one unit is connected
(auto-resolved); otherwise required and validated — `receive_message()` only returns a message
matching both the requested `opcode` and resolved `unit_name`.

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
  `stop_on_receive()`/`stop()`. Callbacks run on an **executor thread, never the event loop**
  (a callback that called back into the sync API from the loop thread would deadlock).
  Exceptions inside callbacks are logged, not propagated.

- A route is either polled or handled, **never both** — mixing raises `RuntimeError`.

### 5b. Class-based handlers (`BaseUnitHandler`)

`handlers.py` adds a declarative alternative to calling `handle_on_receive()` by hand for every
opcode a unit answers: subclass `BaseUnitHandler`, set `unitCode` to the *peer's* configured code
(the "theirs" code from section 3 above, not this process's own), and tag each handling method
with `@route(opCode=...)`:

```python
class TestHandler(BaseUnitHandler):
    unitCode = 0x01

    @route(opCode=0xFFFF)
    def handle_message(self, message):
        self.unitConnection.send_message(reply, REPLY_OPCODE)

manager.create("unit_name", config_data, handler_class=TestHandler)
```

`@route` is a pure marker (tags the function with the opcode); `BaseUnitHandler.__init_subclass__`
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
`config.connected_units`, which is only what was *configured*). Protocol classes report
transitions on the loop thread:

| protocol | connected when | disconnected when |
| --- | --- | --- |
| TCP server | `_on_client` accepts a peer | that peer's read loop ends |
| TCP client | `open_connection` succeeds | its read loop ends |
| UDP client | `_do_start` (remote_addr known) | unit disconnect |
| UDP server | first inbound datagram (`_remember_peer`) | unit disconnect |
| Multicast / DDS | `_do_start` (group join / entities built) | unit disconnect |

`_mark_unit_connected` / `_mark_unit_disconnected` are idempotent and are the **only** things that
arm or disarm a unit's echo. Every transition fires `_notify_state_change()`, which *swaps in* a
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

### 6. The echo lifecycle

Parsed by `config.EchoSettings`:

| key | meaning | default |
| --- | --- | --- |
| `echo_opcode` | one opcode for both directions | -- |
| `recv_echo_opcode` / `send_echo_opcode` | distinct inbound/outbound opcodes | -- |
| `EchoInterval` (`echo_interval`) | seconds between outbound echoes | `1.0` |
| `EchoTimeout` (`echo_timeout`) | seconds of silence before the unit is dropped | `5.0` |
| `echo_payload` | body of the periodic echo | `b""` |

Stays inactive for a unit unless both of that unit's opcodes resolve. `EchoTimeout` must exceed
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
- `EchoInterval` / `EchoTimeout` / `echo_payload` resolve **individually**: a unit overriding only
  its timeout still wants the shared interval, and `timeout > interval` is checked on the merge.

Missing at both levels means echo stays off **for that unit alone** — the same "absent means
disabled" rule as before, now applied per unit instead of per connection. An explicit `null`
(SilentUnit above) is the opt-out: it overrides the global value like any other, and an absent
opcode disables the heartbeat.

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
3. **Watchdog** — if no echo within `EchoTimeout`, disconnects just that unit: cancels its
   periodic senders, fails any parked `receive_message()` with `ConnectionError`, drops standing
   callbacks, closes its socket. Other units on the same connection are untouched. Only on
   `can_receive` connections. Sleeps until each unit's deadline (`last echo + EchoTimeout`)
   rather than polling a fixed tick, so worst-case detection is exactly `EchoTimeout`, not ~2x.

A unit that drops has its echo tasks cancelled and its liveness entry cleared; a peer that comes
back re-arms both through the same `_mark_unit_connected` path, so reconnection needs no special
case. `_last_echo_at` is seeded at connect time, not at `start()`.

An echo send that raises `ConnectionError` retires the unit on the spot (`_mark_unit_disconnected`
then return) rather than warning and retrying: the link is provably gone. This closes the race
with the read loop noticing the same thing — whichever gets there first wins, the other is a
no-op. Other exceptions keep the old behaviour (log, retry next tick, let the watchdog decide).

### 6a. Periodic sending

```python
connection.periodic_sending(opcode: int, data: bytes, interval: int | float,
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
member with the same signature as a plain `Connection`.

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

`Connection.stop()` closes every socket/transport (`_do_stop()`), cancels and awaits every
tracked background task, then cancels any `receive_message()` calls still parked on a
subscription. Every async task the framework starts (read loops, echo replies/senders/watchdogs,
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

### 9. DDS: IDL and QoS files

`dds.py` accepts `idl_file` and `qos_file` paths (plus optional `qos_profile`, `topics`, `types`).

- **IDL answers *what*.** `idl_file` is a **Python module** using `rti.types` decorators, not a
  text `.idl` — no code-generation step. `@idl.struct` builds the TypeSupport Connext uses to
  serialize and publish during discovery. The module is imported by path (in an executor, since
  import runs arbitrary code) and the class handed straight to `dds.Topic(...)`, which is what
  actually registers the type with the participant. The module is cached in `sys.modules`
  because DDS type identity is per-class — re-importing would mint two distinct types with the
  same name. `config["types"]` maps each unit to its class name; a class missing `@idl.struct` is
  rejected with a message naming the class and file.
- **QoS answers *how*.** `dds.QosProvider(qos_file)` parses profiles; nothing applies until a
  profile is pulled per entity kind (`participant_qos_from_profile`, `topic_qos_from_profile`,
  `datawriter_qos_from_profile`, `datareader_qos_from_profile`) and passed to that entity's
  **constructor** (QoS is largely immutable afterwards). Writer/reader QoS mismatch means
  discovery's RxO check fails silently — no error, no data — hence sharing one profile.
