# CLAUDE.md -- core/connections

Modular, JSON-configured connection library (TCP, UDP, Multicast, RTI Connext DDS), Python 3.10. Imported as `core.connections`; it is a library, not an application. Cross-package rules are in the root `CLAUDE.md`.

## Core Commands

- Tests (from repo root): `.venv/Scripts/python.exe -m pytest`.
- Import sanity check (from repo root): `.venv/Scripts/python.exe -c "import core.connections"`.
- From another directory, set `PYTHONPATH` to the repo root first.
- No lint/build step. DDS support self-disables (`DdsConnection = None`) when the RTI Python API is missing.

## Architecture & Tech Stack

### Layout and ownership
- `config.py`: JSON -> typed, frozen `ConnectionConfig`; unit<->port mapping; echo and structures resolution.
- `framing.py`: `(UnitCode, OpCode, DataLength)` little-endian header. `base.py`: event-loop bridge, `Connection` ABC, `FramedConnection`, per-unit state, the `Unit` Protocol.
- `_routes.py`: `RouteTable`; a route has exactly one owner (subscription XOR callback). `_echo.py`: per-unit heartbeat supervisor. `handlers.py`: declarative `UnitHandler`, `@route`, `@on_connect`.
- `tcp.py`, `udp.py`, `multicast.py`, `dds.py`, `dds_config.py`, `composite.py`, `manager.py` (factory plus `shutdown_all()`).
- `connections` owns transports, routing and lifecycle. It does not own the payload codec (`IRS.irs_parser.irs_to_bytes`/`parse_irs`) or helpers (`tools.general`, `tools.file_functions`).
- `tools` answers what an opcode or unit code is *written as*; this package answers what it must *fit into*. Coercion is `tools`'; range checks stay here.

### Sync/async boundary
- One background thread owns one asyncio loop for the whole process (`base._EventLoopThread`). All I/O is coroutines on it.
- The public API is blocking and synchronous; callers never import `asyncio`.
- Callbacks (`handle_on_receive`, `handle_on_connect`) run on an executor thread, never the loop, so they may call the sync API without deadlock. Exceptions in them are logged, not propagated.

### Framing and codec
- `FramedConnection` mixes framing into TCP/UDP/Multicast. `DdsConnection` never touches `framing.py`; it passes typed samples through.
- `_encode` calls `irs_to_bytes` with **our** unit code; `_decode` calls `parse_irs` with the **sender's** code. Raw `bytes` pass through; `parse_irs` returning `None` delivers raw bytes.
- A send that IRS cannot encode raises `IRSDataError`. A receive that IRS cannot parse is logged with the traceback and dropped; one bad frame never costs the read loop, the link or a parked `receive_message()`.
- DDS answers the "could this connection ever deliver this route?" question through the abstract `Connection._validate_route()`. Every subclass must implement it.

### Structures are per-link
- A structures file describes one link, usually both directions. Multicast is the exception. `IRS.REGISTRY` keys by namespace (the structures module's `__name__`) first, so two peers sharing our own unit code cannot overwrite each other.
- `Structures` is declared per unit and resolved at load (`resolve_structures`, same default/override shape as `EchoSettings.resolve`, resolved as a group). Every IRS call takes that scope, read through `_structures_for(unit_name)`.
- A connection-level `Structures` is legal only with exactly one unit, or on multicast; `from_json` rejects it otherwise.
- An empty scope means unscoped (all modules searched). An unscoped lookup matching two modules raises `IRSAmbiguousError`, which must not subclass `IRSNotFoundError`.
- `PAIR_REGISTRY` is a whole-unit alias, namespaced the same way. A unit with its own layouts is never redirected.
- `tools.general.resolve_module_name` is the single source of the namespace, shared by `ConnectionConfig` and `import_modules`. A `.py` path is always loaded from that path.

### Unit routing
- Top-level `unitCode` is OUR code (required, uint8): stamped into every header and passed to `irs_to_bytes`.
- `connections[name].unitCode` is THEIR code (required, range- and collision-checked; ports unique too): it keys subscriptions/callbacks/periodic tasks and is passed to `parse_irs`.
- Inbound routing comes from the transport, not the header. `ConnectionConfig` is `frozen=True, slots=True`; mutating it would desync `Connection`'s caches.

### Sending and receiving
- `opcode` is optional on send only when `data` is an `IrsMessage` carrying its own (an explicit one must agree, else `ValueError`); required for dict/bytes (`TypeError`). Same for `periodic_sending`.
- `unit_name` may be omitted only when exactly one unit is connected. `receive_message()` matches both opcode and resolved unit.
- The selector is turned into a route key by `_message_key`, `_send_key` and `_send_unit`. DDS overrides all three (selector = topic class, sample or name) and `_resolve_route`.
- Subscribe-or-drop: no buffering. `_dispatch_incoming` consumes echoes first, then hands the message to the route owner; if none exists the message is discarded.
- One future per route; a second concurrent `receive_message()` raises `RuntimeError`. A route is polled or handled, never both.
- `trigger_function` arms the subscription before running the trigger, closing the request/response race. If it raises, the subscription is released.
- `handle_on_receive` registrations survive `close()`. A unit already carrying a callback must be released with `stop_on_receive`/`stop_on_connect` first.
- `UnitHandler.__init_subclass__` builds `cls._routes` at class-definition time; duplicate opcodes or duplicate `@on_connect` raise `TypeError` there. Installing a handler only calls `handle_on_receive`/`handle_on_connect` per route; there is no extra dispatch tier.
- `create(..., handler_class=...)` installs before the connection is registered, so bad config fails atomically.
- `periodic_sending` encodes once at schedule time; calling it again for one route replaces the sender. A failed tick is logged and retried.

### Per-unit state and `on_connect`
- `_active_units` tracks live peers (distinct from configured units). `_mark_unit_connected`/`_mark_unit_disconnected` are idempotent and are the only things that arm/disarm echo or fire `on_connect`.
- `on_connect` does not fire retroactively; callers registering on an already-connected unit must handle that themselves.
- DDS has no per-unit transport: `_do_disconnect_unit` closes nothing and echo is rejected on DDS configs.
- Every transition calls `_notify_state_change()`, which swaps in a fresh `asyncio.Event` rather than clearing the old one.
- `wait_for_connected_units` raises `ValueError` for unknown names or counts above what is configured.

### Echo lifecycle
- Keys: `echo_opcode`, `recv_echo_opcode`/`send_echo_opcode`, `EchoInterval`, `EchoTimeout` (`timeout > interval`, else `ValueError`). Defaults live in `config.EchoSettings`. The heartbeat body is always empty.
- Every key is accepted at connection level and per unit. The three opcode keys resolve as a group; interval and timeout resolve individually. An explicit `null` opcode is the per-unit opt-out.
- Resolution happens once in `from_json` into `UnitEndpoint.echo`; malformed keys are load-time `ValueError`s. `Connection` reads it via `_echo_for(unit_name)`.
- Echo arms per unit on connect, never on `start()`. Parts: periodic sender (`can_send` only), consumption in `_dispatch_incoming` (never visible to `receive_message`, never answered), and a watchdog (`can_receive` only; disconnects only that unit).
- An echo send raising `ConnectionError` retires the unit immediately via `_disconnect_unit`.

### CompositeUnit
- Combines direction-limited connections via composition. `MulticastConnection` derives `can_send`/`can_receive` from `config.side`.
- `CompositeUnit.__init__` raises on an ambiguous or impossible send/receive pair. `create_composite` registers only the composite, and nothing on failure.

### Lifecycle
- `close()` closes transports, cancels and awaits every task tracked via `self._track()`, then fails parked `receive_message()` with `ConnectionError`. `_closing` stops dispatch and reconnects.
- A `_do_start` that fails part-way is undone via `_abort_start`. `ConnectionManager.shutdown_all()` closes in reverse creation order, tolerating individual failures.
- `tcp.py`: close peer writers *before* awaiting `Server.wait_closed()` (Python <= 3.12 hang). A peer reset is logged at INFO, not as a traceback.
- `_EventLoopThread._handle_loop_exception` demotes one narrow Windows Proactor teardown artifact to DEBUG; everything else goes to the default handler.

### DDS connection
- A node is one unit of a generated DDS Interface; the JSON names `protocol`, `unit` and `dds_interface`. Interface vocabulary and sender rules: `core/DDS/CLAUDE.md`.
- `dds_config.resolve_unit` (via `_from_dds_json`) derives unit code, peers and `TopicSpec`s at load. `config.dds` holds them; `side`/`ip`/`local_ip` are `None`. A DDS config naming any key it does not read is refused, never silently ignored.
- Route keys are `(unit_code, topic_name)`. An int selector on DDS is a `TypeError`.
- Defaults are absolute constants in `dds_config`: `DEFAULT_DOMAIN_ID`, `DEFAULT_QOS_FILE`, `DEFAULT_LICENSE_FILE`. `domain_id`, `qos_file`, `qos_profile` override. The license is exported as `RTI_LICENSE_FILE` at the top of `dds.py` before `rti` is imported, only if the file exists and the env var is unset.
- QoS applies at construction; RxO mismatch fails silently. Use `_qos_for(entity, topic)` (`set_topic_*_qos`/`get_topic_*_qos`), never the profile-only accessors that ignore `topic_filter`.
- Samples from a peer the Interface does not list are warned about once and dropped. `_validate_route`/`_do_send` raise on our own mismatch. Self-reception is filtered by `A_systemId == own code`.
- `can_send`/`can_receive` are unions over topic directions. `_do_send` takes a typed sample and stamps `A_systemId` only if unset.
- Teardown: cancel read loops first, then one `participant.close()`. `import rti.asyncio` is required (it patches `take_data_async`); `rti.asyncio.close()` runs only for the last live `DdsConnection`.

## Code Style & Constraints

- Fail loudly, but only where the fault is ours: an inbound message IRS does not define is logged and dropped; our own subscription to a nonexistent message raises at the subscribing call.
- `logger.exception` is reserved for faults that should not happen; normal peer disconnects log at INFO.
- Every async task goes through `self._track()` so one sweep tears everything down.
- Keep the public API synchronous end to end.

## Reference Codebases

- Config shapes (units, echo, Structures): `core/tests/test_config.py`, `core/tests/test_echo.py`, `core/tests/test_irs_scope.py`.
- Handlers: `core/connections/handlers.py`, `core/tests/test_handlers.py`, `core/tests/test_on_connect.py`.
- Receive/trigger patterns: `core/tests/test_dispatch.py`, `core/tests/test_tcp.py`. Periodic sending: `core/tests/test_periodic.py`. Composites: `core/tests/test_composite.py`.
- Registry and signatures: `core/IRS/REGISTRY.py`, `core/connections/base.py`.
- DDS: `core/tests/test_dds.py`, `core/DDS/Interfaces/Example/example_interface.py`.
