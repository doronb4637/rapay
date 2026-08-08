# connection_framework

A modular, JSON-configured connection management system for TCP, UDP,
Multicast and RTI Connext DDS, targeting Python 3.10.

## Layout

```
connection_framework/
  config.py      ConnectionConfig: JSON -> typed config, unit<->port mapping
  framing.py      (UnitCode,OpCode,DataLength) little-endian struct pack/unpack
  base.py         _EventLoopThread (sync/async bridge) + Connection ABC
                  + subscribe-or-drop delivery + per-unit state + echo handling
  tcp.py          TcpConnection
  udp.py          UdpConnection
  multicast.py    MulticastConnection (direction derived from config.side)
  dds.py          DdsConnection (RTI Connext; native payloads, no framing)
  composite.py    CompositeUnit -- combines direction-limited connections into one Unit
  manager.py      ConnectionManager -- factory + centralized absolute-shutdown
```

## 0. What this package does not own

Transports, routing and lifecycle live here. The payload codec and the
project's generic helpers do not, and are imported from their own packages:

- **`IRS`** -- `irs_to_bytes` / `parse_irs`, used by `base.Connection._encode`
  and `._decode`. There is no local `irs_parser.py` any more; the package
  re-exports both names, so `from connections import parse_irs` still resolves
  to exactly the same functions.
- **`tools.general`** -- `validated_opCode` normalises every opcode entering
  the framework (`send_message`, `periodic_sending`, `stop_periodic`, and
  `config._as_opcode`); `validated_unitCode` does the same for both kinds of
  unit code in `config._as_unit_code`; `import_modules` loads a config's
  `libs_path` message libraries in `ConnectionManager`, which is what
  populates `IRS.REGISTRY` before any connection exists to use it.
- **`tools.file_functions.readUnitConfig`** -- turns a unit configuration
  *name* into its JSON, so `mgr.create("radar", "TcpServer")` reads
  `config/Units/TcpServer.json` and this package never builds a config path
  itself.

The division with `tools` is deliberate and consistent: `tools` says what an
opcode or unit code is *written as* (so `99` and `"0x63"` are the same value
project-wide), `framing.py` says what it has to *fit into*. Coercion is theirs;
the uint16/uint8 range checks stay here.

## 1. The sync/async boundary

Everything runs on **one** background thread that owns **one** asyncio event
loop for the whole process (`base._EventLoopThread`, a lazily-created
singleton). All actual socket I/O happens as coroutines on that loop; the
public API (`Connection.start/stop/send_message/receive_message`) is plain,
blocking, synchronous Python that marshals each call onto the loop thread
with `asyncio.run_coroutine_threadsafe(...).result(timeout=...)`. The caller
never imports `asyncio`. See the previous revision's notes for the full
rationale and the `asyncio.Queue`-binding subtlety this design already
accounts for.

## 2. Message framing

`framing.py` pre-compiles the header format once at import time via
`struct.Struct("<BHH")` (little-endian: uint8 UnitCode, uint16 OpCode, uint16
DataLength) rather than re-parsing the format string on every call.
`MessageHeader` is a frozen, `slots=True` dataclass. `pack_message` /
`unpack_message` (renamed from `pack_frame`/`unpack_frame`) and `IRSDataError`
(renamed from `FramingError`) round out the module. `FramedConnection` (in
`base.py`) mixes this framing into `TcpConnection`, `UdpConnection` and
`MulticastConnection`; `DdsConnection` never touches this module.

## 2a. IRS payload parsing

The header stays this framework's business -- TCP needs `DataLength` to find
message boundaries at all -- and everything after it belongs to the `IRS`
package. `FramedConnection` sets `uses_irs_parser = True`, which turns
on the codec at both boundaries:

```python
connection.send_message(Track(track_id=7, label="alpha"), opcode=60)
unit, track = connection.receive_message(60, timeout=3)   # -> a Track object
```

- **Outbound** (`send_message`, `periodic_sending`): `Connection._encode` calls
  `irs_to_bytes(own_unit_code, opcode, message)` -- **our** code, per §3 --
  before the result is framed and transmitted. Raw `bytes` are taken as
  already-encoded and pass through untouched, which is how a caller that
  assembles its own payload, and the config-supplied echo payload, still work
  unchanged. `periodic_sending` encodes once, when the schedule is created, so
  a message that can never be encoded fails in the caller's thread instead of
  logging forever in the background.
- **Inbound** (`receive_message`, `handle_on_receive`): `Connection._decode`
  calls `parse_irs(their_unit_code, opcode, payload)` -- the **sender's** code,
  since that is what says how the bytes are laid out -- and the resulting
  message object is what the caller receives. Decoding happens in
  `_dispatch_incoming` *after* a route owner is found, so a message nobody
  subscribed to is dropped without paying to parse it.
- **Both directions wrap the parser in `try`/`except` and re-raise as
  `IRSDataError`**, naming the unit code and opcode that failed, so a parser
  bug never surfaces as a bare `KeyError` from somewhere inside the framework.
- **Failure is per-message, never per-connection.** On receive that
  `IRSDataError` is caught one level up, logged as a warning, and that single
  message is discarded; the read loop keeps running and the next valid message
  arrives normally.
- `parse_irs` returning `None` -- the state of the shipped template -- means
  "no conversion available", and the raw bytes are delivered as-is. A 2-tuple
  is unwrapped to its second element, since `parse_irs` returns
  `(message_name, message_object)` and the object is what the caller wants.

`DdsConnection` leaves `uses_irs_parser = False`: its payloads are typed DDS
samples, and both codec hooks become no-ops for it.

## 3. Unit routing: our code vs their code

There are two kinds of unit code in a config, and telling them apart is the
whole point:

```json
"unitCode": 3,
"connections": {
  "RadarUnit":   {"port": 5000, "unitCode": 7},
  "TrackerUnit": {"port": 5001, "unitCode": 8}
}
```

The **top-level `"unitCode"` is our own** -- who this process is on the wire.
It is **required** (`ConnectionConfig.from_json` raises `ValueError` without
it, because there is no sane default for "who am I"), range-checked as a uint8,
and it is the value stamped into the header of every message we send and passed
to `irs_to_bytes` when encoding one. A peer reading our message learns who sent
it; stamping the *destination's* code there would tell them only what they
already knew.

Each **`"connections"[name]["unitCode"]` is theirs** -- the remote unit's
identity. That is what every routing table keys on, and what `parse_irs` is
handed when decoding something that unit sent us.

So in a correctly configured pair, one side's own `unitCode` equals the code
the other side lists for it, and the same number is used to encode a message
and to parse it: the sender's. Inbound routing itself still comes from the
transport (which socket or port a message arrived on) rather than from the
header, so a mislabelled header cannot misroute anything.

It is **required** -- `ConnectionConfig.from_json` raises `ValueError` if it's
missing or empty, and there is no "default"/anonymous-unit fallback. There is
no separate port list to keep in sync with it; `config.ports` is derived.
`unitCode` is optional (defaulting to the low byte of the port) but is always
range-checked and collision-checked, because two units sharing a code would
collapse into one routing slot. Lookups: `endpoint_for(name)`,
`unit_code_for(name)`, `unit_from_port(port)`, `port_for_unit(name)`.

`ConnectionConfig` is a `frozen=True, slots=True` dataclass: a live
`Connection` caches state derived from it (unit codes, echo settings) and
hands it to read loops on the event-loop thread, so mutating it afterwards
would leave those caches describing a configuration that no longer exists.

## 4. opcode: mandatory on send, subscription key on receive

Every message now carries an `opcode`, and the API is:

```python
connection.send_message(data: bytes, opcode: int, unit_name: str | None = None) -> None
connection.receive_message(opcode: int, unit_name: str | None = None,
                            timeout: float | int | None = None,
                            trigger_function: Callable[[], Any] | None = None) -> tuple[str, bytes]
connection.handle_on_receive(opcode: int, callback_func: Callable[[bytes], Any],
                             unit_name: str | None = None) -> None
connection.stop_on_receive(opcode: int, unit_name: str | None = None) -> bool
```

`opcode` is **mandatory** on `send_message` -- every message declares what
kind of message it is. `unit_name` stays optional wherever exactly one unit
is connected (auto-resolved from `config.connections`); otherwise it's required
and validated, with strict unit filtering: a `receive_message()` call only
ever returns a message matching **both** the requested `opcode` and the
resolved `unit_name`.

## 5. Subscribe-or-drop message filtering

The system does **not** buffer incoming messages indefinitely. `Connection`
keeps `self._subscriptions: dict[(unit_code, opcode), asyncio.Future]`.
Calling `receive_message(opcode, unit_name)` is what "subscribes" the
connection -- it registers a future under that exact key and blocks until
either a matching message arrives or the timeout fires.

Two properties of that key matter:

- It is keyed by the numeric **`unit_code`**, not the unit name. The code is
  what actually travels in the wire header, so routing on it means the
  subscription table and the bytes on the wire can never disagree. Names are
  resolved to codes once, at construction, and `Connection._build_unit_codes`
  rejects a config where two units would collapse onto the same code.
- It holds **one future**, not a list. A given route has at most one
  subscriber at a time; a second concurrent `receive_message()` for the same
  route raises `RuntimeError` rather than silently stranding the first caller.

Every subclass's read loop / datagram callback now calls a single method,
`self._dispatch_incoming(unit_name, opcode, payload)`, instead of pushing
onto a general queue. That method:

1. Consumes echoes first (see below).
2. Otherwise, hands the message to whoever owns that route: a parked
   `receive_message()` future if one is in flight, else a standing
   `handle_on_receive()` callback. **If neither exists, the message is
   discarded immediately** -- it is never queued on the chance some future
   call might ask for it later.

### 5a. `trigger_function`: closing the request/response race

Under subscribe-or-drop, "send a request, then wait for the reply" is a race:
if the reply arrives before the `receive_message()` call registers, it is
dropped and gone. Passing the soliciting call as `trigger_function` removes
the race -- `receive_message` arms the subscription **first**, then runs the
trigger, then blocks:

```python
unit, reply = asker.receive_message(
    REPLY_OPCODE, timeout=3,
    trigger_function=lambda: asker.send_message(b"ping", REQUEST_OPCODE),
)
```

The trigger runs in the caller's own thread, so it may freely use this
connection's sync API; if it raises, the subscription is released and the
exception propagates unchanged.

### 5b. `handle_on_receive`: standing callbacks

Where `receive_message` subscribes, takes one message and unsubscribes,
`handle_on_receive(opcode, callback_func, unit_name)` registers
`callback_func(payload)` permanently for that route until `stop_on_receive()`
or `stop()`.

Callbacks run **on an executor thread, never on the event loop**. That is not
an optimization: user callbacks may block, and one that called back into the
sync API from the loop thread would deadlock -- that call marshals onto the
loop thread and waits for it, but the loop thread is what's running the
callback. An exception inside a callback is logged, not propagated into the
read loop.

A route is either polled or handled, **never both**: registering a callback
over an in-flight `receive_message` (or vice versa) raises `RuntimeError`
rather than silently deciding which one wins.

Without a trigger or a callback, a receiver must already be polling (its
`receive_message()` call already in flight) at the moment a message arrives,
exactly like a real "active subscription" model -- see `test_framework.py`'s
`_receive_in_background` helper, which starts the blocking receive on a
background thread and gives it a moment to register before the sender fires.

## 5c. Per-unit connection state, and waiting on it

`Connection` tracks which units currently have a *live peer* in
`self._active_units`. This is deliberately not the same thing as
`config.connected_units`, which only says what was configured; a unit is
"connected" here when the protocol layer says it has somewhere real to send.
Each protocol reports that transition at the only moment it can actually know
it:

| protocol | connected when | disconnected when |
| --- | --- | --- |
| TCP server | `_on_client` accepts a peer | that peer's read loop ends |
| TCP client | `open_connection` succeeds | its read loop ends |
| UDP client | `_do_start` -- `remote_addr` is known up front | unit disconnect |
| UDP server | the first inbound datagram (`_remember_peer`) | unit disconnect |
| Multicast / DDS | `_do_start` -- group joined / entities built | unit disconnect |

`_mark_unit_connected()` / `_mark_unit_disconnected()` are idempotent, run on
the loop thread, and are the single trigger for the echo lifecycle (§6).

```python
connection.wait_for_connected_units(target: int | str | list[str],
                                    timeout: float | int | None = None) -> bool
connection.active_units   # -> set[str], a snapshot
```

`target` may be a count (`2`), one unit name (`"RadarUnit"`), or a list of
names. The call blocks the caller's thread and returns `True` once the
condition holds, `False` if `timeout` expires first; it returns immediately if
the condition is already satisfied.

There is no polling anywhere in this path. Every state change calls
`_notify_state_change()`, which *swaps in* a fresh `asyncio.Event` rather than
setting-and-clearing one. That detail is what makes concurrent waiters safe: a
waiter parked on the old event is woken by it, and no waiter can consume
another's wake-up by clearing the shared event first.

Targets that could never be met -- an unknown unit name, or a count larger
than the number of configured units -- raise `ValueError` immediately, in the
caller's own thread, rather than becoming a wait that quietly expires.
`CompositeUnit` forwards the call to each member in turn against one shared
deadline, so a composite is "connected" only once both of its directions are.

## 6. The echo lifecycle

Parsed and validated once by `config.EchoSettings`:

| key | meaning | default |
| --- | --- | --- |
| `echo_opcode` | one opcode for **both** directions | -- |
| `recv_echo_opcode` / `send_echo_opcode` | distinct inbound/outbound opcodes | -- |
| `EchoInterval` | seconds between outbound echoes | `1.0` |
| `EchoTimeout` | seconds of silence before the unit is dropped | `5.0` |
| `echo_payload` | body of the periodic echo | `b""` |

`EchoInterval`/`EchoTimeout` are also accepted as `echo_interval`/
`echo_timeout`. The feature stays inactive for a unit unless both of that
unit's opcodes resolve. `EchoTimeout` must exceed `EchoInterval`, or the link
would be declared dead before the next echo was even due -- that's a
config-time `ValueError`, naming the unit when it came from a unit block.

### 6-i. Hierarchical echo config: connection default, per-unit override

Echo is a property of a *link*, not of a process. A connection multiplexing
several units may be talking to peers that heartbeat on different opcodes, at
different rates -- or to one peer that heartbeats and one that doesn't. So
every key in the table above is accepted at **both** levels: in `extra`, where
it is the shared default, and inside an individual unit's dict in
`connections`, where it overrides that default for that unit alone.

```json
"unitCode": 3,
"connections": {
  "RadarUnit":   {"port": 5000, "unitCode": 7, "echo_opcode": 10},
  "TrackerUnit": {"port": 5001, "unitCode": 8},
  "SilentUnit":  {"port": 5002, "unitCode": 9, "echo_opcode": null}
},
"echo_opcode": 99,
"EchoInterval": 1.0,
"EchoTimeout": 5.0
```

RadarUnit heartbeats on opcode 10; TrackerUnit falls back to the
connection-wide 99; SilentUnit opts out entirely. All three share the 1.0s /
5.0s timings, because only the opcode was overridden.

`EchoSettings.resolve(unit_spec, extra)` merges the two levels, at two
different granularities, and the difference is the whole point:

- The three **opcode** keys resolve as a **group**. A unit that names any of
  them is describing its entire heartbeat, so the connection-level opcodes drop
  out completely rather than half-applying. Merging them key-by-key would let a
  unit configured `{"echo_opcode": 10}` sit under a global
  `{"recv_echo_opcode": 99}` and end up *receiving* on 99 while *sending* on
  10 -- a link neither peer ever configured, and one that would look like a
  working config right up until the watchdog fired.
- `EchoInterval` / `EchoTimeout` / `echo_payload` resolve **individually**,
  because each is independently meaningful: a unit overriding only its timeout
  still wants the shared interval. The `timeout > interval` check then runs on
  whatever the merge produced, so an override can't quietly create the
  dead-before-due config the check exists to prevent.

Absent at both levels, echo simply stays off for that unit -- the same "absent
means disabled" rule as before, applied per unit instead of per connection. An
explicit `null` is the deliberate opt-out: it overrides the inherited value
like any other value would, and an absent opcode disables the heartbeat.

Resolution happens once, in `ConnectionConfig.from_json`, and the result is
stored on `UnitEndpoint.echo` (read via `config.echo_for(unit)` or
`config.unit_echoes`). Doing it at load time rather than when a peer connects
preserves the invariant the rest of the framework relies on: a malformed echo
key is a `ValueError` while the config is being read, never a heartbeat that
silently never starts on a link that looks up. `Connection` caches the whole
mapping as `self._unit_echo` and reads one unit's settings through
`self._echo_for(unit_name)` -- on the connect path, and in
`_dispatch_incoming`, which is why an opcode that is a heartbeat on one unit
remains an ordinary application message on the unit next to it. `self._echo`
stays the connection-level block, serving as the fallback for any unit the
config never named.

Three pieces, all per-unit and all armed **when that unit's peer actually
connects** -- not by `start()`. `start()` only means "the transport is up": a
TCP server that is listening has no peer yet, and echoing at one produces
nothing but a failed send every interval, followed by a watchdog "timeout" on a
link that was never up in the first place. Tying the echo to
`_mark_unit_connected` (§5c) instead means each unit's heartbeat begins when it
has somewhere to go, stops the moment the peer drops, and starts again by
itself when the peer returns -- with no reconnection special case anywhere:

1. **Periodic sender** -- transmits the echo opcode every `EchoInterval`
   seconds for as long as the unit stays connected, whether or not anything
   was received. Only started on connections that `can_send`. This is the sole
   owner of the outbound direction. It re-checks `_active_units` each tick, so
   a peer lost between ticks costs at most one no-op wake-up rather than a
   failed send.
2. **Consumption** -- `_dispatch_incoming` intercepts any inbound message
   whose opcode equals `recv_echo_opcode` *before* the subscribe-or-drop
   check runs. It refreshes that unit's liveness timestamp and goes no
   further: never visible to any `receive_message()` caller, even one
   subscribed to that exact opcode, and **no reply is sent**. Replying would
   duplicate what the periodic sender already does on its own schedule, and
   with a single shared `echo_opcode` it would have both peers answering each
   other's answers without end.
3. **Watchdog** -- if no echo arrives from a unit within `EchoTimeout`, that
   unit is disconnected: its periodic senders are cancelled, any parked
   `receive_message()` for it fails with `ConnectionError` instead of sitting
   out its full timeout, its standing callbacks are dropped, and
   `_do_disconnect_unit()` closes just that unit's socket. Other units on the
   same connection are untouched. Only started on connections that
   `can_receive`.

A unit that goes down has its echo tasks cancelled and its liveness entry
cleared, and `_last_echo_at` is seeded from the moment the peer appears rather
than from `start()` -- otherwise a unit that connected late would be measured
against a clock that had been running since before it existed.

An echo send that raises `ConnectionError` retires the unit immediately instead
of logging a warning and trying again next tick: that error means the link is
provably gone, so there is nothing to retry. Both the failed send and the read
loop noticing the same disconnect end at `_mark_unit_disconnected`, which is
idempotent -- whichever observes it first wins and the other becomes a no-op,
which is what removes the "echo send failed: Connection lost" window during an
ordinary disconnect. Any *other* exception keeps the original behaviour: log
it, keep trying, and let the watchdog be the thing that gives up.

The watchdog sleeps until each unit's *deadline* (`last echo + EchoTimeout`)
rather than polling on a fixed tick. Waking earlier only to find the deadline
hasn't passed is wasted work, and waking on a fixed `EchoTimeout` tick would
push worst-case detection out to nearly 2x the timeout (an echo landing just
before a tick resets the clock, but the next check is still a full timeout
away). Sleeping to the deadline wakes at most once per timeout period *and*
detects the death at the deadline itself -- the test suite observes the
disconnect at exactly `EchoTimeout`.

## 6a. Periodic sending

```python
connection.periodic_sending(opcode: int, data: bytes, interval: int | float,
                            unit_name: str | None = None) -> None
connection.stop_periodic(opcode: int, unit_name: str | None = None) -> bool
```

`periodic_sending` behaves exactly like `send_message` but keeps sending in
the background every `interval` seconds. Tasks are tracked in
`self._periodic_tasks`, keyed by the same `(unit_code, opcode)` route key as
subscriptions. Calling it twice for one route **replaces** the sender -- the
old task is cancelled and awaited before the new one is stored, so two
senders can never overlap and quietly double the rate. A send that fails is
logged and retried next tick rather than killing a schedule the caller
believes is still running. `stop_periodic` returns whether anything was
actually running. Both are forwarded by `CompositeUnit` to its send-capable
member.

## 7. The Composite Connection Challenge

`CompositeUnit` (`composite.py`) combines several direction-limited
connections into one logical Unit via composition, not monkey-patching. Every
`Connection` exposes `can_send` / `can_receive` flags; `MulticastConnection`
now derives these **entirely from `config.side`**:

```
Side.SENDER   -> send-only
Side.RECEIVER -> receive-only
anything else -> duplex
```

(There is no `mode`/`duplex` key in `config.extra` for multicast any more --
`UdpConnection` still supports its own `mode` extra flag, which is how the
sandboxed test below stands in for a real send-only Multicast link.)

`CompositeUnit.__init__` picks the one send-capable and one receive-capable
member from its `members` list and raises immediately if that's ambiguous or
impossible. `send_message`/`receive_message` on the composite simply
delegate to the right member, forwarding the same `opcode`/`unit_name`/
`timeout` signature as a plain `Connection`.

```python
beacon = mgr.create_composite("BeaconUnit", {
    "transport": {"protocol": "multicast", "side": "sender", ...},
    "receive": {"protocol": "udp", "side": "server", "mode": "receive_only", ...},
})
beacon.start()
beacon.send_message(b"...", opcode=20)
unit, payload = beacon.receive_message(opCode=21)
beacon.close()
```

> **Sandbox note:** `test_framework.py`'s composite demo uses two directional
> **UDP** links because this execution sandbox's network namespace has no
> multicast routing (confirmed independently with raw sockets). `multicast.py`
> is a complete, working implementation -- swap the `"transport"` member's
> `"protocol"` to `"multicast"` and its `"side"` to `"sender"` (and the peer's
> to `"receiver"`) on a network that supports multicast, and nothing else in
> the calling code changes.

## 8. Lifecycle: absolute teardown

`Connection.stop()` closes every socket/transport (`_do_stop()`), then
cancels and `await`s every tracked background task, and finally cancels any
`receive_message()` calls still parked on a subscription so they don't hang
forever. Every async task the framework starts -- read loops, in-flight echo
replies, periodic echo senders, echo watchdogs, and `periodic_sending`
schedules -- goes through `self._track()`, so a single sweep over
`self._tasks` covers all of them; `_periodic_tasks` and `_echo_tasks` hold
the *same* Task objects and are only cleared of their stale entries.
`ConnectionManager.shutdown_all()` does this for every managed connection in
reverse creation order, tolerating individual failures. One real Python
<=3.12 bug is documented and fixed in `tcp.py`:
`asyncio.Server.wait_closed()` blocks until every accepted connection has
*also* finished, so peer writers must be closed **before** awaiting it.

Two further details concern events that are *normal* for a network link but
used to be reported as failures:

- `TcpConnection._read_loop` catches `OSError` on its own, before the catch-all
  `except Exception`. A peer that vanishes rather than closing politely -- an
  RST (`WinError 64`/`10054`), a killed process, an interface dropping -- is
  logged at INFO exactly like a graceful close. `logger.exception` and its
  traceback stay reserved for things that genuinely should not happen, so a
  traceback in the log remains a real signal.
- `_EventLoopThread` installs a loop exception handler that demotes exactly one
  artifact to DEBUG. `_ProactorBasePipeTransport._call_connection_lost` calls
  `sock.shutdown()` on a socket whose peer is already gone; CPython guards that
  call for `ConnectionResetError` only, so on Windows an RST makes asyncio
  report an unhandled `OSError` *after* the transport is already dead -- there
  is nothing to act on and nothing is lost. The match is deliberately narrow
  (that callback, an `OSError`, one of four Windows codes); every other
  context, including any other `OSError`, is passed to
  `loop.default_exception_handler` unchanged.

## 9. DDS: IDL and QoS files

`dds.py` accepts `idl_file` and `qos_file` paths (plus optional
`qos_profile`, `topics`, `types`) and documents inline exactly how RTI
Connext consumes them. The short version:

- **IDL answers *what*.** `idl_file` is a **Python module** using the
  `rti.types` decorators, not a text `.idl`:

  ```python
  import rti.types as idl

  @idl.struct
  class Point:
      x: int = 0
      y: int = 0
  ```

  `@idl.struct` builds the TypeSupport Connext uses to serialize instances
  and to publish the type during discovery, so there is no code-generation
  step at all -- the module is imported by path (in an executor, since an
  import executes arbitrary module-level code) and the class handed straight
  to `dds.Topic(...)`. Creating the `Topic` is what actually **registers**
  the type with the participant. The module is cached in `sys.modules`
  because DDS type identity is per-class: re-importing would mint two
  distinct types with the same name. `config["types"]` maps each unit to its
  class name; a class missing `@idl.struct` is rejected with a message
  naming the class and file rather than failing deep inside Connext.
- **QoS answers *how*.** `dds.QosProvider(qos_file)` parses the profiles;
  nothing is applied until a profile is pulled out per entity kind
  (`participant_qos_from_profile`, `topic_qos_from_profile`,
  `datawriter_qos_from_profile`, `datareader_qos_from_profile`) and passed to
  that entity's **constructor** -- QoS is largely immutable afterwards, which
  is why every entity is built with its QoS in hand.

Writer/reader QoS is what discovery checks for RxO compatibility: mismatch it
and the pair simply never connects, with no error and no data -- hence one
shared profile.

## Verified

`test_framework.py` exercises, end-to-end over real loopback sockets:

- TCP: multi-port server + client, header framing, mandatory `opcode`,
  multi-unit routing, bidirectional round trip, absolute teardown.
- UDP: single implicit unit (`unit_name` optional, `opcode` still required).
- CompositeUnit: send-only + receive-only links combined into one Unit,
  direction enforcement, absolute teardown of both members.
- Subscribe-or-drop filtering: a message sent before anyone is subscribed is
  confirmed dropped, not delivered to a later `receive_message()` call.
- Echo consumption: an inbound echo refreshes liveness, never reaches the
  application via `receive_message()`, and triggers no reply.
- `trigger_function`: a request/response round trip completes in a single
  `receive_message()` call with no background thread and no sleep-before-send;
  a trigger that raises propagates unchanged and releases the route.
- `handle_on_receive`: a standing callback answers repeated requests, calls
  back into the sync API from its executor thread without deadlocking, and is
  removed by `stop_on_receive()`; polling and handling one route at once is
  rejected.
- Periodic echo with a single shared `echo_opcode`: both peers stay alive
  across ~5 intervals, and the measured send count proves no echo storm.
- `EchoTimeout`: a unit whose peer never answers is disconnected
  automatically, and subsequent sends on it are refused.
- `periodic_sending` / `stop_periodic`: repeated delivery, task *replacement*
  on a repeat call for the same route, and confirmed silence after stopping.
- One subscriber per route: a second concurrent `receive_message()` for the
  same `(unit_code, opcode)` is rejected with `RuntimeError`.
- Hierarchical echo config: per-unit opcodes override the connection-level
  ones, units without any inherit them, `null` opts a unit out, and the
  timing keys stay shared through all of it; a unit's opcode keys replace the
  global set as a group rather than half of it; a merge that violates
  `EchoTimeout > EchoInterval` fails at load naming the unit. Behaviourally:
  two units on one TCP server heartbeat on their own opcodes, and one opcode
  is consumed as an echo on one unit while being delivered as application
  data on the other.
- Echo lifecycle vs. real peer state: a two-unit TCP server sends no echo and
  drops no unit while nothing is connected, arms unit1's echo alone when
  unit1's client connects (unit2 stays independently disarmed), holds the link
  up past `EchoTimeout` on that heartbeat, stops echoing when the peer closes,
  and resumes automatically on reconnect.
- `wait_for_connected_units`: `int`/`str`/`list[str]` targets, `False` on
  timeout, `ValueError` on impossible targets, and a waiter parked before the
  final unit connects is released the moment it does.
- IRS parsing: an application object survives a round trip as an object, a
  payload the parser rejects is logged and dropped while the connection keeps
  working, and `handle_on_receive`/`periodic_sending` use the same codec.

Run it with `python3 test_framework.py`.
