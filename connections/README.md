# connection_framework

A modular, JSON-configured connection management system for TCP, UDP,
Multicast and RTI Connext DDS, targeting Python 3.10.

## Layout

```
connection_framework/
  config.py      ConnectionConfig: JSON -> typed config, unit<->port mapping
  framing.py      (UnitCode,OpCode,DataLength) little-endian struct pack/unpack
  base.py         _EventLoopThread (sync/async bridge) + Connection ABC
                  + subscribe-or-drop delivery + automatic echo handling
  tcp.py          TcpConnection
  udp.py          UdpConnection
  multicast.py    MulticastConnection (direction derived from config.side)
  dds.py          DdsConnection (RTI Connext; native payloads, no framing)
  composite.py    CompositeUnit -- combines direction-limited connections into one Unit
  manager.py      ConnectionManager -- factory + centralized absolute-shutdown
```

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

## 3. Unit routing

A single `"connections"` block is the source of truth: it maps each
connection name (the logical unit) to the port it lives on and the unit code
it identifies itself with on the wire.

```json
"connections": {
  "RadarUnit":   {"port": 5000, "unitCode": 7},
  "TrackerUnit": {"port": 5001, "unitCode": 8}
}
```

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

## 6. The echo lifecycle

Configured entirely from `config.extra`, parsed and validated once by
`config.EchoSettings`:

| key | meaning | default |
| --- | --- | --- |
| `echo_opcode` | one opcode for **both** directions | -- |
| `recv_echo_opcode` / `send_echo_opcode` | distinct inbound/outbound opcodes | -- |
| `EchoInterval` | seconds between outbound echoes | `1.0` |
| `EchoTimeout` | seconds of silence before the unit is dropped | `5.0` |
| `echo_payload` | body of the periodic echo | `b""` |

`EchoInterval`/`EchoTimeout` are also accepted as `echo_interval`/
`echo_timeout`. The feature stays inactive unless both opcodes resolve.
`EchoTimeout` must exceed `EchoInterval`, or the link would be declared dead
before the next echo was even due -- that's a config-time `ValueError`.

Three pieces, all per-unit and all started by `start()` once the transport is
actually up:

1. **Periodic sender** -- transmits the echo opcode every `EchoInterval`
   seconds *unconditionally*, whether or not anything was received. Only
   started on connections that `can_send`. This is the sole owner of the
   outbound direction.
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
    "receive":   {"protocol": "udp", "side": "server", "mode": "receive_only", ...},
})
beacon.start()
beacon.send_message(b"...", opcode=20)
unit, payload = beacon.receive_message(opcode=21)
beacon.stop()
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

Run it with `python3 test_framework.py`.
