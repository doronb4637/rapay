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

`ConnectionConfig.unit_map` **must** be explicitly supplied via the JSON
`"units"` block -- `ConnectionConfig.from_json` raises `ValueError` if it's
missing or empty. There is no "default"/anonymous-unit fallback anywhere in
the codebase anymore. Lookups are `unit_from_port(port)` and
`port_for_unit(name)`.

## 4. op_code: mandatory on send, subscription key on receive

Every message now carries an `op_code`, and the API is:

```python
connection.send_message(data: bytes, op_code: int, unit_name: str | None = None) -> None
connection.receive_message(op_code: int, unit_name: str | None = None,
                            timeout: float | int | None = None) -> tuple[str, bytes]
```

`op_code` is **mandatory** on `send_message` -- every message declares what
kind of message it is. `unit_name` stays optional wherever exactly one unit
is connected (auto-resolved from `config.unit_map`); otherwise it's required
and validated, with strict unit filtering: a `receive_message()` call only
ever returns a message matching **both** the requested `op_code` and the
resolved `unit_name`.

## 5. Subscribe-or-drop message filtering

The system does **not** buffer incoming messages indefinitely. `Connection`
keeps `self._subscriptions: dict[(unit_name, op_code), list[asyncio.Future]]`.
Calling `receive_message(op_code, unit_name)` is what "subscribes" the
connection -- it registers a future under that exact key and blocks until
either a matching message arrives or the timeout fires.

Every subclass's read loop / datagram callback now calls a single method,
`self._dispatch_incoming(unit_name, op_code, payload)`, instead of pushing
onto a general queue. That method:

1. Checks automatic echo handling first (see below).
2. Otherwise, looks up `(unit_name, op_code)` in `_subscriptions`. If a
   waiting future is found, the message is delivered to it. **If nothing is
   subscribed, the message is discarded immediately** -- it is never queued
   on the chance some future call might ask for it later.

This means a receiver must already be polling (its `receive_message()` call
already in flight) at the moment a message arrives, exactly like a real
"active subscription" model -- see `test_framework.py`'s
`_receive_in_background` helper, which starts the blocking receive on a
background thread and gives it a moment to register before the sender fires.

## 6. Automatic echo handling

`config.extra` may optionally define `"received_echo_opcode"` and
`"sent_echo_opcode"` (both integers). If **either** is missing, the feature
stays inactive and every message goes through ordinary subscribe-or-drop
delivery. If both are present, `_dispatch_incoming` intercepts any inbound
message whose `op_code` equals `received_echo_opcode` *before* the
subscribe-or-drop check ever runs: it automatically calls `_do_send(...,
sent_echo_opcode)` to reply (tracked via `self._track()` so it participates
in absolute teardown like any other background task), and the original
message is silently dropped -- it is never visible to any `receive_message()`
caller, even one actively subscribed to that exact `op_code`.

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
delegate to the right member, forwarding the same `op_code`/`unit_name`/
`timeout` signature as a plain `Connection`.

```python
beacon = mgr.create_composite("BeaconUnit", {
    "transport": {"protocol": "multicast", "side": "sender", ...},
    "receive":   {"protocol": "udp", "side": "server", "mode": "receive_only", ...},
})
beacon.start()
beacon.send_message(b"...", op_code=20)
unit, payload = beacon.receive_message(op_code=21)
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
cancels and `await`s every tracked background task (read loops, in-flight
echo replies), and finally cancels any `receive_message()` calls still
parked on a subscription so they don't hang forever. `ConnectionManager.
shutdown_all()` does this for every managed connection in reverse creation
order, tolerating individual failures. One real Python <=3.12 bug is
documented and fixed in `tcp.py`: `asyncio.Server.wait_closed()` blocks
until every accepted connection has *also* finished, so peer writers must be
closed **before** awaiting it, not after.

## Verified

`test_framework.py` exercises, end-to-end over real loopback sockets:

- TCP: multi-port server + client, header framing, mandatory `op_code`,
  multi-unit routing, bidirectional round trip, absolute teardown.
- UDP: single implicit unit (`unit_name` optional, `op_code` still required).
- CompositeUnit: send-only + receive-only links combined into one Unit,
  direction enforcement, absolute teardown of both members.
- Subscribe-or-drop filtering: a message sent before anyone is subscribed is
  confirmed dropped, not delivered to a later `receive_message()` call.
- Automatic echo handling: a configured echo request is answered
  automatically and never reaches the application via `receive_message()`.

Run it with `python3 test_framework.py`.
