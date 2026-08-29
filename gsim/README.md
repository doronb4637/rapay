# GSim — Generic Simulator

A UI and an HTTP/WebSocket API on top of the `core` connection framework.

## Architectural constraint: core is untouched

Not one file under `core/` was modified. The dependency is confined to a single
package — `gsim/core_gateway/` — which is **the only place in GSim that imports
`connections`, `IRS` or `tools`**. Everything above it talks to that package.

```
<repo-root>/
  core/                      ← UNTOUCHED. connections/ IRS/ tools/ annotations.py
  gsim/
    __main__.py              PyWebView desktop launcher (`python -m gsim`)
    core_gateway/            ◄── THE ONLY IMPORTER OF core
      bootstrap.py             puts <repo-root> on sys.path (imports first)
      schema.py                IRS message class -> JSON form schema
      registry.py              read-only view of the GLOBAL MESSAGE REGISTRY
      payloads.py              zero-fill + counted-array sync before encoding
      runtime.py               GSim's connection registry, logs, thread bridge
    api/
      app.py                   FastAPI factory (+ serves the built UI)
      models.py                Pydantic contract — where GSim is STRICTER than core
      routes/
        connections.py         yellow box: create / edit / delete / start / stop
        messages.py            gray box: registry query, form schema, send, logs
        events.py              WebSocket: live log + state feed
    web/                     React 18 + Vite + Tailwind v4 + lucide-react
      public/fonts/            Inter + JetBrains Mono woff2 (see the README there)
      src/lib/
        schema.js              form defaults, sizes, andeach leaf's byte offset
        bytes.js               ◄── payload -> bytes, mirroring IRS's encoder
        format.js              the one clock + hex formatter the whole UI uses
        prefs.js               persisted panel widths / collapsed sections
      src/components/
        ui.jsx                 shared design-system primitives
        FieldRenderer.jsx      ◄── recursive dynamic form (enums, arrays, bitfields)
        ByteRuler.jsx          ◄── the payload's bytes, lit by field
        Inspector.jsx          compose | inspect
        LinkOverview.jsx       the workspace's resting view: this unit's links
        Console.jsx            unified sent+received log stream, hide-by-opCode
        Sidebar.jsx            connections list
        Resizer.jsx            draggable divider between the three columns
        ConnectionModal.jsx    create/edit modal
        MessagesTable.jsx      messages a selected connection may send
```

Grep for `import connections` or `import IRS` outside `core_gateway/` and you
get nothing — that is the isolation guarantee, and it is mechanically checkable.

## Running

```bash
# desktop app (starts the API on a free loopback port, opens a native window)
.venv/Scripts/python.exe -m gsim

# headless API only, for external programs / development
.venv/Scripts/python.exe -m gsim --server        # http://127.0.0.1:8765/docs

# UI dev server with hot reload (proxies /api and /ws to :8765)
cd gsim/web && npm install && npm run dev

# production bundle — FastAPI serves gsim/web/dist at "/" when it exists
cd gsim/web && npm run build
```

## Design notes

### The byte ruler, and why the encoder is mirrored in the browser

The Inspector shows the payload's actual bytes under the message header, with a
field's range lighting up as you point at it and the reverse. `web/src/lib/
bytes.js` produces those bytes in the browser rather than asking the server for
them, which needs justifying, because core is the only thing that actually
encodes what goes on the wire.

It is a mirror in the same sense `schema.js`'s `defaultPayload` already was: a
read-out, never a trust boundary. `Structure.to_bytes` composes one
`struct.Struct` per field and concatenates with no padding, so a field's wire
position is the sum of the widths before it -- and every input that needs is
already in the form schema. The one thing that used to be an assumption,
endianness, is not one any more: `core_gateway/schema.py` reads it off each
field's own packer format and carries it as `endian`. If the mirror and core
ever disagree, core is right and the mirror is the bug.

What this buys, beyond seeing the bytes: zero-fill stops being a footnote in the
compose footer and becomes visible, a counted array's length byte can be watched
tracking its list, and `inspect` mode gets a job that is not "compose, greyed
out".


### Threading: why every core-touching route is `def`, not `async def`

`core`'s public API is **blocking and synchronous** by design — `send_message`,
`start` and `close` each marshal onto core's single background event loop
(`_EventLoopThread`) and block the caller on the result. Declaring those routes
`async def` would run them *on* the ASGI event loop and stall every other
request, including the WebSocket streaming the logs. As plain `def`, Starlette
runs each in its threadpool, where blocking is expected. The only `async def`
handler is the WebSocket, which does no core work at all.

Inbound messages come the other way: `Connection._run_callback` invokes handlers
via `run_in_executor(...)`, so callbacks land on arbitrary worker threads with no
event loop. `runtime.EventBus.publish` hops back with
`loop.call_soon_threadsafe`, which is what gets a received message onto the
WebSocket.

### Where GSim is deliberately stricter than core

Core accepts a config with no `Structures` key — correct for core, since a
byte-oriented deployment genuinely needs no message layouts. It is wrong for
GSim: with no registered layouts the Messages panel has nothing to list and the
Inspector has no schema to build a form from, so the connection would be created
and then be useless. `gsim/api/models.py` therefore makes it **mandatory at the
API edge**, and core stays unchanged. Peer `unitCode` uniqueness and
side/protocol agreement are enforced there too, so the modal shows a field-level
error instead of surfacing a `ValueError` string from deep inside `from_json`.

### Two IRS behaviours the wrapper must absorb

Both were verified against the real encoder, and both are why `payloads.py`
exists rather than passing form output straight through:

1. **A missing field is not a clean error.** `Structure.from_dict` only assigns
   keys that are present, leaving the slot unset; `to_bytes` then raises a bare
   `AttributeError: 'SetGeneralFlag' object has no attribute 'value'` from inside
   IRS. (`BitField.from_dict` is stricter still — `KeyError`.) Zero-filling every
   absent field is what makes the spec's "empty fields default to 0" real.

2. **A counted array's length field is not maintained by IRS.**
   `ArrayField.to_bytes` iterates the list and never writes the count, while
   `from_bytes` reads exactly `getattr(instance, count_field)` items. A mismatch
   raises nothing — the receiver simply mis-parses. GSim reconciles the two on
   every send, and the UI renders the counter read-only so it cannot be desynced
   by hand.

What GSim does *not* have to do: convert the form dict into an IRS object.
`irs_to_bytes` accepts a plain dict and calls `message_class.from_dict()`
itself, and `Connection._encode` passes any non-`bytes` straight to it — so the
Inspector's state *is* the payload.

### 'Edit' rebuilds, it does not mutate

`ConnectionConfig` is `frozen=True, slots=True` and a live `Connection` caches
state derived from it, so an in-place edit would desync those caches. `PUT`
deletes and recreates, preserving running state.

### 'Delete' without touching core

`ConnectionManager` has no per-connection removal and `_connections` is private,
so GSim keeps **its own registry** as the UI's source of truth and uses the
manager as a factory plus an exit-time safety net. Delete closes the connection
and drops GSim's record; the manager's now-inert entry is harmless because
`Connection.close()` is idempotent, making its eventual `shutdown_all()` a no-op
on that one.

### Received messages are labelled with the *sender's* configured name

One connection can serve several peers, each named in `connections`. Core's
callbacks are invoked as `callback(message)` with no route context, so
`runtime._install_receive_handlers` registers one handler per `(peer, opCode)`
and closes over the peer's **configured name** — which is exactly what the
Received log must display. The opCode set for each peer comes from the GLOBAL
REGISTRY keyed by that peer's unit code, the same lookup `parse_irs` performs,
so every registered route is one core will accept.

A log entry also carries the `unit_code` its layout is registered under — ours
on send, the peer's on receive — because the Inspector must fetch the schema
under the sender's code to render an inbound message correctly.

### The console spans every connection

A send is logged against the SENDER's record and its matching receive against
the RECIPIENT's -- two different GSim connections. A console scoped to the
selected connection can therefore only ever show one half of an exchange, so
both panes read process-wide (`GET /api/logs/{direction}` for backfill, plus
every `message.*` socket event unfiltered). Each row carries
`connection_name` so it stays clear which connection owns it.

### Hide by opCode

Sent and Received are separate panes, each with its own hidden-opCode set.
Independent sets matter because this project's IRS layouts routinely reuse one
opcode for a request and its acknowledgement, so muting your own outbound
traffic must not blind you to the replies it provokes. Holding the set as
opCodes rather than marking entries is what makes "all current and future
messages" fall out for free: new entries are filtered on render, so nothing has
to be re-marked as it streams in.

## Behaviours

A behaviour is a rule: **when** something happens, **then** send a message. Open one from the
Compose footer or from a row in the Behaviours panel.

### When — the trigger

| trigger | fires | send once |
| --- | --- | --- |
| **periodic sending** | as soon as the behaviour and its connection are both running | not offered — it is the schedule itself |
| **the unit connects** | the moment that peer has a usable link — handshakes, init bursts | default |
| **a message arrives** | on each matching message from that peer | default |

The unit picker only appears when the connection has more than one peer; with one, its name is
printed in the sentence instead.

"The unit connects" fires immediately if the peer is *already* connected when you save the rule —
core's on-connect callback only arms the next transition, so waiting for one would look like the
rule was broken.

### Then — the action

**Send once**, or **periodic sending** every N seconds. "The unit connects" and "a message arrives"
default to send once — the ordinary request/response shape — and offer periodic sending as a
deliberate second choice. "Periodic sending" as a *trigger* has no event to repeat on, so under it
send once is not offered at all. Reactive triggers also take a **wait** in milliseconds before
sending — a response latency, honoured to well under a millisecond rather than rounded up to the
Windows timer tick.

A periodic action started by "a message arrives" **restarts** on each matching message, carrying
whatever values that message brought. One schedule, never two — re-triggering replaces it.

### Only if — the condition

"A message arrives" takes an optional field check: `only if Len > 3`, `only if Flag is ON`. Arrivals
that fail it are counted as skipped rather than silently ignored, so a typo in a condition does not
look like a dead link.

Enums compare by **member name** (`ON`), not by number, and only with `is` / `is not` — ordering
member names alphabetically means nothing.

### Copy from the incoming message

Forward values from the message that triggered the rule into the one you send: echo a transaction
id, mirror a state. Each row is `source field → target field`, and once traffic flows it shows the
**last value that actually travelled it** — a mapping reading a field that is not there looks
exactly like a working one until it shows you a number.

Both ends must be the same kind: an enum travels as a member name and a number as a number, so
copying one into the other is refused when you save it, not once per message on a worker thread.

### What a rule can address

Conditions and mappings name a field by dotted path (`Header.Mode`), reaching into structs and into
a bitfield's individual bits. **Nothing inside a repeating array** — a single path cannot name one
of 35 elements, so those fields are not offered.

### Rules per message

One rule per outbound message *per trigger*, so "greet on connect" and "answer a poll" can both
send the same message. Two always-on periodic schedules on one message still collide, because that
would silently double its rate.

Every send goes through the same path as the Send button, so it appears in the Sent console with a
real timestamp — including reactive ones. Behaviours are not saved in session files.

## Known core issues GSim works around (no changes made)

- `tools.file_functions.read_unit_config` is an unimplemented stub (`...`), so
  `ConnectionManager.create(name, "SomeConfigName")` — the `str` form — returns
  `None` and fails. GSim always passes a config **dict**, which is both the
  working path and the one that lets the UI build configs.
- `core/connections/CLAUDE.md` still carries a stale "known blocker" note about
  `validated_opCode`/`validated_unitCode` rejecting ints; they handle ints
  correctly today. Left alone, as it is inside `core/`.
