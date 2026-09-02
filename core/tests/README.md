# `connections` test suite

A `pytest` suite for the `connections` package (`config.py`, `base.py`,
`_routes.py`, `_echo.py`, `handlers.py`, `manager.py`, `composite.py`,
`framing.py`, `tcp.py`, `udp.py`),
independent of `connections/test_framework.py` (that script stays as-is; this
suite is the pytest-native counterpart, with its own fixtures and its own
dedicated IRS message range so the two never collide).

`core` is an ordinary Python package rooted at `<repo-root>/` (`core/__init__.py`), so `connections`,
`IRS`, `tools` and this `tests` package are all reached as `core.connections`, `core.IRS`,
`core.tools`, `core.tests` -- absolute imports through `core`, not top-level packages of their own.

## Running

Use the project's `.venv` (Python 3.11, already has `pytest` and `rti.connext`
installed) from the **repo root**, not from inside `core/`:

```bash
.venv/Scripts/python.exe -m pytest
```

`pytest.ini` lives at the repo root and sets `pythonpath = .` / `testpaths = core/tests`, so
`core.connections`/`core.IRS`/`core.tools`/`core.annotations` resolve the same way they do for
`core/connections/test_framework.py`, without an installed package. `pytest core` (an explicit path)
works too, but plain `pytest` from the repo root is what `testpaths` is there for.

Skip the slower, real-timing echo tests during iteration:

```bash
.venv/Scripts/python.exe -m pytest -m "not slow"
```

DDS is exercised by `test_dds.py` (`rti.connext` is installed in this `.venv`),
which covers the surrogate topic opcodes, config validation, QoS `topic_filter`
resolution, type lookup, header extraction and routing -- all without a live
domain. The one test that puts a real `DomainParticipant` on one is gated behind
`requires_license`: creating a participant needs an RTI license, and a machine
without one must not read as a code failure. Separately,
`test_manager.py::test_create_rejects_unregistered_protocol` self-skips when it
detects DDS is registered, since that scenario needs a genuinely unregistered
protocol to prove anything. True IP multicast stays out of
scope regardless of environment: this sandbox's network namespace has no
multicast routing, the same constraint `connections/test_framework.py`
documents; `test_composite.py` uses two directional UDP links in its place,
exactly like that script does.

If you ever run this under a *different* Python (not the provided `.venv`) --
e.g. a bare `python`/3.14 on PATH -- be aware `IRS`'s struct/buffer parsing
decodes `Text.data` (and friends) as `str` instead of `list[int]`/bytes on
3.14, which breaks `connections/test_framework.py` too, independent of
anything in this suite or in `connections` itself. Stick to the `.venv`.

## Layout

| file | covers |
| --- | --- |
| `conftest.py` | `manager` (auto-`shutdown_all()`), `free_port`/`free_ports`, `receive_in_background` |
| `_messages.py` | This suite's own registered IRS layouts (unit codes 200-219 -- never overlaps `IRS.Structures.Test.test_messages`'s 1-162) |
| `test_framing.py` | Pure header pack/unpack, no I/O |
| `test_config.py` | `ConnectionConfig.from_json` validation/coercion, `EchoSettings` resolution -- pure, no event loop |
| `test_routes.py` | `RouteTable`: route ownership, subscription-XOR-callback exclusivity, per-unit and whole-connection teardown -- pure, synchronous |
| `test_echo_supervisor.py` | `UnitEchoSupervisor` against a stub `EchoHost` on a private loop: arming, sending, consumption, the watchdog's deadline -- millisecond timings, no sockets |
| `test_handlers.py` | `route`/`UnitHandler` class-definition-time logic, `install_handler` wiring |
| `test_manager.py` | `ConnectionManager` factory, lifecycle, `Structures` import normalization |
| `test_dispatch.py` | Subscribe-or-drop, `handle_on_receive`, `trigger_function`, mutual exclusion, echo consumption, malformed-payload handling, per-unit `_disconnect_unit` cleanup |
| `test_periodic.py` | `periodic_sending`/`stop_periodic`: repetition, replace-don't-double, interval validation, cancellation on `close()` |
| `test_on_connect.py` | `handle_on_connect`/`stop_on_connect` and the `@on_connect` handler tag: exclusivity, no retroactive firing |
| `test_echo.py` | Real periodic-sender/watchdog timing, per-unit hierarchy, isolated per-unit disconnect, re-arming after reconnect (marked `slow`) |
| `test_tcp.py` | Multi-port server, stream reassembly, peer supersession, disconnect |
| `test_udp.py` | Implicit single-unit, `mode` restriction, peer learning, malformed datagrams |
| `test_composite.py` | Direction-limited member combination, construction validation, partial-teardown tolerance |

## Fixture design notes

- **`manager`** (function-scoped) always calls `shutdown_all()` on teardown,
  even if the test raised mid-assertion -- no test can leak a live
  socket/echo task into the next one, which matters more here than in
  `test_framework.py`'s manual `try/finally`s because pytest runs the whole
  suite in one process.
- **`free_port`/`free_ports`** bind ephemeral sockets to grab real, currently-free
  OS ports rather than hardcoding numbers -- avoids the whack-a-mole of picking
  non-colliding literals across dozens of tests.
- **`test_routes.py` / `test_echo_supervisor.py` need no fixtures at all.**
  `RouteTable` and `UnitEchoSupervisor` were extracted out of `Connection`
  precisely so the rules they own -- one owner per route; a heartbeat armed by
  connect and disarmed by disconnect -- could be asked directly instead of
  inferred from whether a live socket pair survived. Anything provable there
  belongs there; the socket-level files stay for what genuinely needs a wire.
- Two connections that both name the same logical peer (e.g. a UDP server and
  its client, both calling it `"Peer"`) must declare the **same** `unitCode`
  for that entry -- IRS selects a message layout by that value, not by
  physical direction, so both ends have to agree on it even though each side
  also has its own distinct top-level `unitCode` (what it stamps into
  outgoing headers). Get this wrong and `validate_irs`/`parse_irs` raises
  `IRSNotFoundError` on whichever side actually tries to decode.
