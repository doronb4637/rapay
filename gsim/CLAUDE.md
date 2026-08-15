# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

GSim (Generic Simulator): a FastAPI + WebSocket API and a React/Tailwind desktop UI built **on
top of** `core` (the `connections`/`IRS`/`tools` framework one directory up). It lets a user
create/edit/delete connections, browse the messages a connection may send, compose and send them
through a schema-driven form, and watch sent/received traffic in a live console.

## The one rule that matters more than any other in this package

**`core/` is never modified.** Every file under `core/` is off-limits — no edits, no monkey-patches,
no reaching into private attributes to "just fix" something. If a change to `core` would genuinely
be the better fix for something GSim needs, that is a conversation to have with the user first, not
a decision to make unilaterally.

> That escape hatch has been used exactly once, with explicit user authorisation: making IRS
> layouts per-link (namespaced by structures module, `Structures` declared per unit — see
> `core/connections/CLAUDE.md` §2b). It could not be done from GSim, because the collision was in
> `IRS.REGISTRY`'s own key shape. It is not a precedent for editing `core` without asking. Everything GSim needs from `core` is obtained by importing it and
wrapping it, never by changing it. This is enforced structurally (see Architecture ยง1) and should be
checked mechanically before any commit:

```bash
grep -rn "^from connections\|^import connections\|^from IRS\|^import IRS\|^from tools\|^import tools" gsim --include=*.py | grep -v core_gateway
# must print nothing
git status --porcelain core   # must be empty / show only pre-existing untracked scaffolding
```

## Commands

```bash
# desktop app: starts the API on a free loopback port, opens a native PyWebView window
.venv/Scripts/python.exe -m gsim

# headless API only (external programs, or UI dev server target)
.venv/Scripts/python.exe -m gsim --server        # http://127.0.0.1:8765/docs

# UI dev server with hot reload -- proxies /api and /ws to :8765 (see vite.config.js)
cd gsim/web && npm install && npm run dev

# production bundle -- FastAPI serves gsim/web/dist at "/" when it exists (app.py)
cd gsim/web && npm run build

# core's own test suite must stay green -- GSim changes should never break it
.venv/Scripts/python.exe -m pytest      # from the repo root; see core/tests/README.md
```

Run Python commands with the repo-root `.venv` (`.venv/Scripts/python.exe`), not a bare `python` --
it is the interpreter with `fastapi`, `pywebview`, `uvicorn`, `rti.connext`, and `pytest` installed.
`gsim/core_gateway/bootstrap.py` puts `<repo-root>/core` on `sys.path` at import time, so `import
connections` works from inside `gsim` without an installed package -- no manual `PYTHONPATH` needed
for anything that imports `gsim` first.

## Layout

```
core/                        UNTOUCHED. connections/ IRS/ tools/ annotations.py -- see core/*/CLAUDE.md
gsim/
  __main__.py                 PyWebView desktop launcher + `--server` headless mode
  core_gateway/                THE ONLY PACKAGE THAT IMPORTS core
    bootstrap.py                puts <repo-root>/core on sys.path (must import first)
    schema.py                   IRS message class -> JSON form schema (recursive)
    registry.py                 read-only, namespace-scoped view of the IRS registry
    payloads.py                 zero-fill + counted-array sync before encoding
    runtime.py                  GSim's connection registry, message logs, thread bridge
  api/
    app.py                      FastAPI factory; serves gsim/web/dist at "/" if built
    models.py                   Pydantic contract -- where GSim is STRICTER than core
    routes/
      connections.py             create / edit / delete / start / stop
      messages.py                registry query, form schema, send, log history
      events.py                  WebSocket: live log + connection-state feed
  web/                         React 18 + Vite 5 + Tailwind v4 + lucide-react
    public/favicon.svg           tab icon (matches the Logo mark)
    src/
      App.jsx                    shell: [Connections/Messages] | Inspector | [Sent/Received]
      api.js                     fetch wrapper + WebSocket client (auto-reconnect)
      lib/schema.js               client-side mirror of payloads.py's defaulting rules
      components/
        ui.jsx                    shared design-system primitives (Button, Field, Badge, ...)
        Logo.jsx                   GSim badge mark (vector; swappable for the PNG)
        Sidebar.jsx                connections list, status-dot toggle, create/edit/delete
        MessagesTable.jsx          messages a selected connection may send (left column)
        Inspector.jsx              compose (editable form) | inspect (read-only) modes
        FieldRenderer.jsx          recursive dynamic-form renderer -- the core of Inspector
        Console.jsx                Sent + Received panes, process-wide, hide-by-opCode
        ConnectionModal.jsx        create/edit connection form (hex codes, IPv4 mask)
```

### What this package depends on

| import | used for |
| --- | --- |
| `connections.manager.ConnectionManager` | `runtime.GSimRuntime` -- the factory GSim builds every connection through |
| `connections.*` (`Connection`, `CompositeUnit`) | type hints only in `runtime.py`; never constructed directly |
| `IRS.irs_parser.{get_message_class,list_routes,known_unit_codes,IRSAmbiguousError}` | `registry.py` -- read-only. Namespace-scoped: the lookup/alias/ambiguity rules are **not** reimplemented here, they are called |
| `IRS.core.{ArrayField,Structure}`, `IRS.bitfields.BitField`, `IRS.fields.{Field,EnumField,BaseField}` | `schema.py` -- introspects `Message._fields_` to build the form schema |
| `IRS.constants` | `schema.py` -- reverse-maps struct format chars (`'H'`) back to dtype names (`UInt16`) |

All five imports are confined to `gsim/core_gateway/`. Nothing in `gsim/api/` or `gsim/web/` imports
`core` directly -- they only call into `core_gateway`'s functions (`get_runtime()`,
`message_schema()`, `build_payload()`, ...).

## Conventions

Same as the rest of the repo (see `core/connections/CLAUDE.md`): fail loudly at the point of the
mistake, comments explain the *why* only where the logic is genuinely non-obvious, no speculative
abstraction. Two additions specific to this package:

- **Every fact this file states about `core`'s behavior was verified by running real code against
  it**, not inferred from reading it (see `core_gateway/payloads.py`'s docstring for the two
  concrete `AttributeError`/silent-corruption cases this uncovered). If you touch `core_gateway` and
  a claim here turns out to be stale, re-verify against the real encoder before trusting either the
  old comment or your assumption -- `core`'s IRS layer has already produced two surprises that
  looked plausible-but-wrong on paper.
- **`gsim/api/models.py` is where GSim is allowed to be stricter than `core`.** If a new requirement
  needs core to accept something it currently rejects (or vice versa), that is the "ask the user
  first" case from the rule at the top of this file -- do not work around it by parsing/mutating
  `core`'s internals from `core_gateway`.

## Architecture

### 1. The isolation boundary: `core_gateway`

`gsim/core_gateway/` is the entire surface area of GSim's dependency on `core`. `bootstrap.py` is
imported first by every other module in the package and puts `<repo-root>/core` on `sys.path` (core's
own internal imports are absolute and rooted at `core/` itself -- `from IRS.irs_parser import ...`,
`from annotations import *` -- so `core/`, not the repo root, has to be the thing on the path).
Everything above `core_gateway` -- `gsim/api/*`, `gsim/web/*` -- talks only to this package's public
functions (`get_runtime()`, `list_messages()`, `message_schema()`, `build_payload()`), never to
`connections`/`IRS`/`tools` directly. This is what makes the isolation grep in the top-of-file rule
meaningful rather than aspirational.

### 2. Threading: why every core-touching route is `def`, not `async def`

`core`'s public API is blocking and synchronous by design (`core/connections/CLAUDE.md` ยง1):
`send_message`, `start`, `close` each marshal onto core's single background event loop
(`_EventLoopThread`) via `await_coroutine(...)` and block the caller until it finishes. Declaring a
FastAPI route `async def` runs it *on* the ASGI event loop; calling a blocking core method from there
would stall every other request, including the WebSocket streaming the console. Every route in
`gsim/api/routes/connections.py` and `messages.py` is therefore plain `def`, so Starlette runs it in
its threadpool, where blocking is exactly what is expected. `gsim/api/routes/events.py`'s WebSocket
handler is the only `async def` in the API, and it does no core work at all -- it only drains an
`asyncio.Queue`.

Inbound messages travel the other direction: `Connection._run_callback` invokes a registered
`handle_on_receive` callback via `run_in_executor(...)`, so a callback body runs on an arbitrary
worker thread with no event loop of its own. `runtime.EventBus.publish` is written to be called from
any thread and hops back onto each subscriber's asyncio loop with `loop.call_soon_threadsafe`, which
is what gets a received message from a core executor thread onto a live WebSocket client. Verified
end-to-end (executor-thread callback -> `EventBus` -> WebSocket frame) before this was trusted.

### 3. Schema introspection: `schema.py`

`describe_message()` reads `Message._fields_` (built once by `IRS.core.MessageMeta` at class
definition time -- never mutated) and recursively produces a JSON-serializable schema:

| `IRS` field type | schema `"kind"` |
| --- | --- |
| `Field` | `"scalar"` -- dtype name + numeric range, reverse-mapped from the struct format char via `IRS.constants` |
| `EnumField` | `"enum"` -- member name/value pairs, renders as a dropdown |
| `Structure` | `"struct"` -- nested `"fields"`, rendered recursively |
| `BitField` | `"bitfield"` -- each packed bit-range as its own scalar/enum entry |
| `ArrayField` | `"array"` -- see below |

An `ArrayField.length` is the load-bearing distinction: `None` -> `"dynamic"` (consumes the rest of
the buffer, grows by user action), a `str` -> `"counted"` (its length lives in a **sibling** field
named by that string), an `int` -> `"fixed"` (exactly N items, no add/remove). This is exactly the
distinction `payloads.py` (ยง4) has to reconcile on the way out.

### 4. Payload normalization: `payloads.py`

Two concrete `core`/`IRS` behaviors make this module mandatory, both confirmed against the real
encoder (not assumed from reading the source):

1. **A missing field is not a clean error.** `Structure.from_dict` only assigns keys that are
   present, leaving the slot unset; `to_bytes` then does `getattr(target, field.name)` and raises a
   bare `AttributeError: 'SetGeneralFlag' object has no attribute 'value'` from inside `IRS`.
   `BitField.from_dict` is stricter still (`data[name]` -> `KeyError`). `payloads.normalise()`
   zero-fills every absent/blank field recursively, which is what makes "empty fields default to 0"
   real rather than aspirational.
2. **A counted array's length field is not maintained by `IRS`.** `ArrayField.to_bytes` iterates the
   list and never writes the count; `from_bytes` reads exactly `getattr(instance, count_field)`
   items on the way back in. A mismatch raises nothing on send -- the receiver just mis-parses.
   `normalise()`'s second pass overwrites the counted field with `len()` of its sibling array after
   every other field has been materialized (order matters: the count field must exist before it can
   be overwritten).

`build_payload(schema, raw)` is the public entry point `gsim/api/routes/messages.py` calls before
`runtime.send()`. Nothing converts the resulting dict into an IRS object: `irs_to_bytes` already
accepts a plain dict and calls `message_class.from_dict()` itself, and `Connection._encode` passes
any non-`bytes` value straight through to it -- so the Inspector's form state *is* the wire payload,
with zero serialization step in GSim.

### 5. `runtime.py`: GSim's own registry, logs, and the thread bridge

`GSimRuntime` is a process-wide singleton (`get_runtime()`) holding:

- **`_records: dict[str, ConnectionRecord]`** -- GSim's own source of truth for "what connections
  exist," independent of `ConnectionManager._connections` (private, and has no per-connection
  removal method). `ConnectionManager` is used purely as a factory (`create()`) plus an exit-time
  safety net (`shutdown_all()` in the FastAPI `lifespan`). Deleting a GSim connection calls
  `Connection.close()` (documented idempotent) and drops GSim's record; the manager's now-inert
  entry is harmless, because its eventual `shutdown_all()` closing an already-closed connection is a
  no-op. **Do not** reach into `ConnectionManager._connections` to "properly" remove the entry --
  that is exactly the kind of `core`-touching workaround the top-of-file rule forbids without asking
  first.
- **`_install_receive_handlers`** -- for each configured peer, looks up every opcode that peer's
  unit code may send (`registry.list_messages(peer_code)`, the same registry lookup order
  `parse_irs` uses) and registers one `handle_on_receive` callback per `(peer, opcode)`, closing over
  the peer's **configured name**. Core's callback signature is `callback(message)` with no route
  context, so this closure is the only place that name is available -- it is what lets the console
  label a received message with the sender's name from the config, not our own connection's name.
- **`LogEntry.unit_code`** -- the unit code a log entry's layout is registered under: our own on
  send, the peer's on receive. The Inspector's "inspect" mode fetches the schema by this code
  (`GET /api/schema/{unit_code}/{op_code}`), never by the connection's own code, because a received
  message was decoded under the *sender's* code (`parse_irs(their_code, ...)`) and asking under the
  wrong one finds the wrong layout or none at all.
- **`EventBus`** -- thread-safe pub/sub (ยง2) that `events.py`'s WebSocket subscribes to.

### 6. Where GSim is deliberately stricter than core: `api/models.py`

`core.ConnectionConfig.from_json` accepts a config with no `Structures` key -- correct for `core`,
since a byte-oriented deployment genuinely needs no message layouts. It would be a dead end for
GSim: with no registered layouts, `MessagesTable` has nothing to list and `Inspector` has no schema
to build a form from, so the connection would exist and be useless. `ConnectionCreate.structures` is
therefore `Field(min_length=1)` -- **mandatory at the API edge**, enforced by Pydantic, with `core`
itself unchanged. `ConnectionCreate._check_consistency` also front-runs two errors `core` would
otherwise raise deep inside `from_json` (duplicate peer `unitCode`s, a `side` invalid for the chosen
`protocol`) so the create/edit modal can show a field-level reason instead of a raw exception string.
`to_core_config()` is the one function that renders GSim's form model into the exact JSON shape
`ConnectionManager.create()` expects -- always a **dict**, never a config *name* string (see ยง8).

### 7. 'Edit' rebuilds, it does not mutate

`ConnectionConfig` is `frozen=True, slots=True`, and a live `Connection` caches state derived from it
at construction -- there is no supported in-place mutation path, and inventing one by reaching into a
running connection's internals is exactly the kind of `core`-touching shortcut the top rule forbids.
`PUT /api/connections/{id}` (`runtime.replace()`) is delete-then-recreate under the hood, preserving
whether the connection was running.

### 8. Frontend: React 18 + Tailwind v4 + lucide-react

Tailwind v4 is CSS-first (`@import 'tailwindcss'` + `@theme` in `styles.css`, wired in via
`@tailwindcss/vite` in `vite.config.js`) -- there is no `tailwind.config.js` to look for. Design
tokens (buttons, inputs, badges, status dots, panel chrome) live in one place, `components/ui.jsx`,
so every panel stays visually consistent and a token change is a one-line edit rather than a
find-and-replace across class strings.

`FieldRenderer.jsx` is the piece the whole Inspector rests on: one component that renders itself for
nested structs and array items, so a struct inside an array inside a struct needs no special case at
any depth. State flows by composition (`value` + `onChange(next)`, narrowed at each level) rather
than by field paths, which is what makes the form's root state *already be* the exact payload dict
`api.send()` submits -- no serialization step on the client either, mirroring ยง4's dict-in/dict-out
design on the server side. `lib/schema.js`'s `defaultFor`/`defaultPayload` are a client-side mirror
of `payloads.normalise()`'s zero-fill rules, kept only so the form starts fully populated and
controlled from first render -- the server re-normalizes every payload before encoding regardless,
so this mirror is not a trust boundary and the two must simply agree, with the server as tiebreaker
if they ever drift.

`Console.jsx` renders two stacked panes, Sent above Received, each with its own independent
hidden-opCode set. Keeping the sets separate matters because this project's IRS layouts routinely
reuse one opcode in both directions (a request and its distinct-message-but-same-opcode
acknowledgement), so muting your own outbound chatter on an opcode must not also blind you to the
replies it provokes.

**Both panes are process-wide, not scoped to the selected connection**, and this is a correctness
requirement rather than a preference. A send is logged against the SENDER's record and the matching
receive against the RECIPIENT's -- two different GSim connections -- so a per-connection console can
only ever show one half of any exchange (send from A to B with A selected, and the inbound copy is
invisible until you go click B; this was a real reported bug). `runtime.all_logs(direction)` backs
`GET /api/logs/{direction}` for the initial backfill, and `App.jsx` subscribes to every
`message.*` socket event without filtering by connection. Each row carries `connection_name` so it
is still clear which connection owns it. Entries are ordered by the server-assigned `seq` -- a single
counter shared across every record -- because wall-clock timestamps originate on different threads
and are not reliably orderable against each other.

### 9. Known `core` issues GSim works around without touching `core`

- `tools.file_functions.read_unit_config` is an unimplemented stub (`...`). The `str` form of
  `ConnectionManager.create(name, "SomeConfigName")` therefore does not work. `to_core_config()`
  (ยง6) always builds and passes a **dict**, which is both the only working path today and the one
  that lets the UI construct configs from form input in the first place.
- `core/connections/CLAUDE.md` still carries a stale "known blocker" note claiming
  `validated_opCode`/`validated_unitCode` reject plain ints; they handle ints correctly as of the
  current `core/tools/general.py`. Noted here, left alone there -- it is inside `core/`.
