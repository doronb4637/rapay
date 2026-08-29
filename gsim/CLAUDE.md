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
> `IRS.REGISTRY`'s own key shape. It is not a precedent for editing `core` without asking.
>
> Used a second time, also with explicit user authorisation: `core/IRS/_alias.py` (+ two lines in
> `core/IRS/__init__.py`) making `IRS.x` and `core.IRS.x` one module object, and
> `tools.general._assert_registered` failing loudly when a structures file registers nothing. The
> fault was inside `core/IRS`'s own module identity and produced two GSim-visible symptoms --
> `IRSNotFoundError` against an empty registry, and "No widget for this IRS field type" for every
> field, because `schema.py`'s `isinstance` ladder was testing against the other copy's classes.
> Neither was fixable from `gsim/`.
>
> Used a third time, also with explicit user authorisation: fixing `IRS`'s `fill()` and the codec
> edges around it (`core/IRS/core.py`, `fields.py`, `bitfields.py`, plus a new
> `core/tests/test_irs_fill.py`), so GSim could delete its own copy of the defaulting rules.
> `fill()` had no callers and no coverage — GSim was its first real consumer, which is what
> surfaced the faults: a fixed array of structs filled with N references to one CLASS-level object
> (editing one element changed every message of that type built afterwards, process-wide);
> `fill()` never descending into a present-but-partial child; `None` being the intended "unset" for
> a no-0-member enum while every other method in the codec rejected it; enum attributes never
> validated at all; and a field with no wire format silently vanishing from `_fields_`. None of it
> was reachable from `gsim/`.

Everything GSim needs from `core` is obtained by importing it and
wrapping it, never by changing it. This is enforced structurally (see Architecture ยง1) and should be
checked mechanically before any commit:

```bash
grep -rn "^from connections\|^import connections\|^from IRS\|^import IRS\|^from tools\|^import tools" gsim --include=*.py | grep -v core_gateway
# must print nothing
git status --porcelain core   # only the authorised changes above; nothing new without asking
```

## Commands

```bash
# desktop app: starts the API on a free loopback port, opens a native PyWebView window
.venv/Scripts/python.exe -m gsim

# headless API only (external programs, or UI dev server target)
.venv/Scripts/python.exe -m gsim --server        # http://127.0.0.1:8765/docs

# UI dev server with hot reload -- proxies /api and /ws to :8765 (see vite.config.js)
cd gsim/web && npm install && npm run dev

# production bundle -- FastAPI serves gsim/web/ui_dist at "/" when it exists (app.py)
cd gsim/web && npm run build

# the shippable Windows installer: UI -> PyInstaller onedir -> dist/GSim-<version>-x64.msi
powershell -ExecutionPolicy Bypass -File scripts/build_installer.ps1

# core's own test suite must stay green -- GSim changes should never break it
.venv/Scripts/python.exe -m pytest      # from the repo root; see core/tests/README.md
```

The installer build uses `.venv3.10`, not `.venv` -- PyInstaller is installed in
that one. Everything else stays on `.venv`.

Run Python commands with the repo-root `.venv` (`.venv/Scripts/python.exe`), not a bare `python` --
it is the interpreter with `fastapi`, `pywebview`, `uvicorn`, `rti.connext`, and `pytest` installed.
`gsim/core_gateway/bootstrap.py` puts the `<repo-root>` on `sys.path` at import time, so `import
core...` works from inside `gsim` without an installed package -- no manual `PYTHONPATH` needed
for anything that imports `gsim` first. It is the repo root, **not** `core/` itself: `core` is an
ordinary package rooted there, and putting `core/` on the path as well is what used to give the
process two `IRS` module objects (see `core/IRS/_alias.py`).

## Layout

```
core/                        UNTOUCHED. connections/ IRS/ tools/ annotations.py -- see core/*/CLAUDE.md
GSim.spec                    PyInstaller recipe (onedir) -- paired with gsim/paths.py, see 14
installer/GSim.wxs           WiX v5 authoring for the MSI (+ License.rtf)
scripts/build_installer.ps1  UI -> .exe -> .msi, in that order
gsim/
  __main__.py                 PyWebView desktop launcher + `--server` headless mode
                                (js_api: browse_structures_file, save_config_file, load_config_file)
  paths.py                    every path that differs between a checkout and the installed app
  core_gateway/                THE ONLY PACKAGE THAT IMPORTS core
    bootstrap.py                puts <repo-root> on sys.path so `core...` resolves (must import first)
    schema.py                   IRS message class -> JSON form schema (recursive)
    registry.py                 read-only, namespace-scoped view of the IRS registry
    payloads.py                 form payload -> a built IRS message, ready to encode
    behaviours.py               scheduled sending (periodic, ...) -- GSim-driven, see 10
    runtime.py                  GSim's connection registry, message logs, thread bridge
  api/
    app.py                      FastAPI factory; serves gsim/web/dist at "/" if built
    models.py                   Pydantic contract -- where GSim is STRICTER than core
    routes/
      connections.py             create / edit / delete / start / stop / import (Save-Load)
      messages.py                registry query, form schema, send, log history + clear
      behaviours.py              list / upsert / start / stop / delete schedules
      events.py                  WebSocket: live log + connection-state feed
  web/                         React 18 + Vite 5 + Tailwind v4 + lucide-react
    public/favicon.svg           tab icon (matches the Logo mark)
    src/
      App.jsx                    shell: [Connections/Messages] | Inspector | [Sent/Received]
      api.js                     fetch wrapper + WebSocket client (auto-reconnect)
      lib/schema.js               client-side mirror of payloads.py's defaulting rules
      lib/sessionFile.js          Save/Load envelope: build, parse, browser-download fallback
      components/
        ui.jsx                    shared design-system primitives (Button, Field, Badge, ...)
        Logo.jsx                   GSim badge mark (vector; swappable for the PNG)
        Sidebar.jsx                connections list, status-dot toggle, create/edit/delete
        MessagesTable.jsx          messages a selected connection may send (left column)
        Inspector.jsx              compose (editable form) | inspect (read-only) modes
        FieldRenderer.jsx          recursive dynamic-form renderer -- the core of Inspector
        Console.jsx                Sent + Received panes, process-wide, hide-by-opCode
        ContextMenu.jsx            desktop-style right-click menu + useContextMenu()
        BehavioursPanel.jsx        every active schedule, across every connection
        BehaviourModal.jsx         configure/stop/remove one message's schedule
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
imported first by every other module in the package and puts the REPO ROOT on `sys.path` (`core` is
an ordinary package rooted there, `core/__init__.py`, and its own internal imports are absolute
through it -- `from core.IRS.irs_parser import ...`, `from core.annotations import *` -- so the repo
root, not `core/` itself, has to be the thing on the path; `CORE_ROOT` stays a plain filesystem path
for `STRUCTURES_DIR`/`CONFIGS_DIR`, unrelated to what's importable).
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

**IRS's `fill()` owns absence.** Every field nobody set gets its safe default at any depth, from
the IRS field objects themselves. GSim used to carry a second copy of that table (`_default_for`,
walking the JSON form schema) written before `Message.fill()` existed; it is gone.

`prepare_message(unit_code, op_code, namespaces, raw)` resolves the route once
(`registry.resolve_route` -- **not** `message_schema`, which builds a recursive widget description
the send path has no use for), runs the small pre-pass below, then
`message_class.from_dict(...).fill()`. It returns the built message, its name, its namespace, and
its `to_dict()` form for the log. `runtime.send()` and `PUT /behaviours` are its only callers.

Three things stay GSim's, because IRS neither can nor should decide them:

1. **Blanks.** An untouched input submits `""`, which is not "unset" to anything in IRS -- it
   reaches `struct.pack` and raises `required argument is not an integer`. Dropping it lets
   `fill()` treat the field as absent, which is what the user meant.
2. **Fixed-array size.** A `[Data, 9]` field given 3 items is a short frame, and core now rejects
   it outright (`ArrayField.to_bytes`). GSim pads and truncates instead: a form should be forgiving
   about how many rows you happened to fill in.
3. **Counted-array lengths.** `ArrayField.to_bytes` never writes the count and `from_bytes` reads
   exactly `getattr(instance, count_field)` items, so a disagreement raises nothing on send -- the
   receiver just mis-parses. The count is DERIVED from the list, never taken from the form. This
   one cannot be fixed in IRS: `to_bytes(writer, value)` has no access to the sibling holding it.

**An enum with no `0` member is left explicitly unset (`None`), not guessed at.** That is what
`EnumField.fill()` returns, `to_bytes` writes it as `0`, `from_bytes` reads a `0` back as `None`
(reachable only when the enum declares no `0` member), and `to_dict`/`from_dict` pass it through.
`web/src/lib/schema.js` mirrors this and `FieldRenderer.jsx` offers an "— unset —" option so the
state is reachable in the form, not just displayable.

A NON-zero value no member defines is a different thing -- a sender contradicting the shared
specification -- and still raises (`ValueError`, wrapped by `Connection._decode` into
`IRSDataError`), so the message is dropped and logged. Bitfield enum *bits* differ deliberately:
they decode to `None` with a deduped warning instead, because `BitField.from_bytes` stores the
whole packed integer without inspecting any bit, so a raise there would come from a property read
long after the decode.

A built message -- not a dict -- is handed to `send_message`: `Connection._encode` passes any
non-`bytes` straight to `irs_to_bytes`, which calls `message.to_bytes()` on a message but repeats
the route lookup and `from_dict` on a dict.

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
- **`LogEntry.payload`** -- always the message's own `to_dict()`, in BOTH directions, so a sent
  entry spells enums as member names (`"ON"`) exactly as a received one always has. The two panes
  used to disagree, sent being the raw numeric form the browser submitted. `resolveEnumValue`
  (`FieldRenderer.jsx`) and `enumValue` (`lib/bytes.js`) normalise both shapes, so this is not a
  client concern -- but `GET /api/logs/sent` and stored behaviour payloads carry the names now.
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

### 6a. A connection's NAME is its identifier

There is no opaque `conn-1` id. `_conn_records` is keyed by name, every route addresses it
(`/api/connections/{connection_name}/...`), and `LogEntry` / `Behaviour` reference it. This was a
deliberate trade, made to keep hand-typed URLs readable — `curl .../api/connections/Tiful/messages`
instead of looking up which number `Tiful` happens to be this run. Three things pay for it:

- **Names must be unique.** Enforced in `runtime.create`/`replace` (not the request model — only the
  runtime knows what exists). A duplicate would not merely confuse: the second connection would take
  over the first's URLs and inherit its behaviours.
- **Names must be URL-safe.** `CONNECTION_NAME_PATTERN` in `api/models.py` allows only
  `[A-Za-z0-9._-]`. A `/` would split the path into segments matching no route; a space cannot be
  typed raw at all — which would defeat the entire point. `import` is additionally reserved, since it
  shares a path position with `POST /api/connections/import`.
- **Renaming is a change of identity.** `replace` accepts current + new name; the connection is
  addressed at a new URL afterwards, and (as with any edit — see §7) its logs and behaviours do not
  survive, because `delete` drops both. A rename onto an existing name is refused *before* anything
  is torn down, so a rejected edit cannot leave the original deleted. `App.jsx` follows the returned
  record so the UI selection does not point at a name that no longer exists.

### 7a. Save/Load a session, and why import bypasses `ConnectionCreate`

The title bar's Save/Load buttons (`App.jsx`, next to the GSim logo -- not the Connections panel's
own header, which is too narrow to reliably fit both without one landing under the Inspector layout)
round-trip the *whole* connections list through a JSON file, not
one connection at a time -- `lib/sessionFile.js` builds `{version, exported_at, connections: [{name,
autostart, config}, ...]}` from `connection.config`, exactly what `GET /api/connections` already
returns per record. Loading re-submits each entry to `POST /api/connections/import`
(`ConnectionImport` in `api/models.py`), which calls `runtime.create()` directly with that raw config
-- it deliberately does **not** go back through `ConnectionCreate`/`to_core_config()`. Re-deriving
`peers`/`structures` from a core config and re-validating it through the stricter frontend contract
would lose any protocol-specific `extra` key the modal doesn't model (`idl_file`, `qos_file`, `ttl`,
`mode`, ...), since `ConnectionCreate.extra` only ever holds what the modal explicitly collected when
the connection was first created. Importing the raw config is what makes Save/Load lossless.

File I/O has two paths, chosen at click time (not cached, unlike the modal's `canBrowse` state) by
checking `window.pywebview?.api?.save_config_file`: the desktop app uses `__main__.py`'s
`save_config_file`/`load_config_file` (native save/open dialogs, read/write directly on disk);
`--server` mode has no such bridge, so Save triggers a client-side `Blob` download and Load clicks a
hidden `<input type="file">` -- both plain browser APIs, no server round trip either way.

### 7b. Where the modal narrows core's own options, and why

Three of the create/edit form's fields are deliberately more restrictive than what core's config
schema would accept, all for the same reason: offering a control for a value nothing downstream
reads is worse than not offering it, because the user has to guess whether it does something.

- **Remote IP is hidden for a listening endpoint** (tcp/udp `side: "server"`, and DDS on any side).
  Confirmed against the protocol classes, not assumed: `TcpConnection`/`UdpConnection` never read
  `config.ip` on the server path (only `local_ip`, to bind), and `DdsConnection` never reads either --
  DDS is topic-based, not socket-based. Multicast keeps the field always, relabeled "Multicast IP",
  because `MulticastConnection` reads `config.ip` as the group address on both `sender` and
  `receiver` sides. The field is submitted regardless (core requires the `ip` key unconditionally) --
  it is pinned to `"0.0.0.0"` rather than asked of the user when hidden.
- **`Structures` always lives on the peer, never on the connection, except multicast.** Core itself
  only *requires* this once there are 2+ units (`connections/CLAUDE.md` 2b); GSim applies it
  uniformly, including the single-peer case, so a connection's shape in the form never changes
  depending on how many peers it happens to have today. `toForm` backfills a single legacy
  connection-level `Structures` onto its one peer when editing a connection saved before this
  applied, so Edit doesn't show it as unset.
- **Per-peer echo settings are additive, not a restriction**: core already resolves `EchoSettings`
  hierarchically (connection-level default, per-unit override -- `EchoSettings.resolve`,
  `core/connections/config.py`), the modal just didn't expose the per-unit half before. `PeerSpec`
  gained explicit `echo_opcode`/`echo_interval`/`echo_timeout` fields (typed and validated the same
  as the connection-level ones) instead of relying on `extra="allow"` passthrough, so a malformed
  peer-level value is a 422 with a field path, not a `ValueError` string from deep in `from_json`.

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
`api.send()` submits -- no serialization step on the client either, mirroring ยง4's dict-in design
on the server side. `lib/schema.js`'s `defaultFor`/`defaultPayload` are a client-side mirror of
IRS's own `fill()`, kept only so the form starts fully populated and controlled from first render
-- the server re-normalizes every payload before encoding regardless, so this mirror is not a trust
boundary and the two must simply agree, with `fill()` as tiebreaker if they ever drift.

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

### 10. Behaviours: scheduled sending, and why core's `periodic_sending` is unused

A behaviour is "keep sending THIS message to THIS peer, like THIS". `core_gateway/behaviours.py`
owns them; `BehaviourEngine` runs one daemon thread per active behaviour and calls
`GSimRuntime.send()` — **the same call the manual Send button makes**.

Core *has* `Connection.periodic_sending(opcode, data, interval, unit_name)`, and it works, but it is
deliberately not used here. Its send loop calls `_do_send` directly on core's own event loop, so it
never passes through `runtime.send()` and GSim would log nothing for a schedule actively producing
traffic — while the *receiving* GSim connection would still log every tick, since inbound callbacks
are unaffected. The console would contradict itself: a silent Sent pane beside a filling Received
pane. It also encodes its payload **once** at schedule time, which makes anything varying per tick
(a counter, a timestamp, jitter) impossible through it by construction — and more behaviour shapes
are the stated direction.

Load-bearing rules:

- **Keyed by route, `(connection_name, unit_name, op_code)`** — at most one schedule per message per
  destination, mirroring what core enforces internally for `_periodic_tasks` and for the same
  reason: two schedules on one route would silently double its rate. `PUT` is therefore an upsert.
- **`enabled AND connection running`** is one condition, evaluated in `BehaviourEngine._sync`. That
  single funnel is what makes stopping a connection pause its behaviours and starting it resume
  them, with no separate paused state to keep in step. `enabled` is intent; `active` is reality, and
  the UI's status dot shows `active` so a behaviour armed on a stopped connection never reads as live.
- **A failing tick never kills the schedule** — the usual cause (peer not yet connected) is
  transient. The error lands on the behaviour (`last_error`, `error_count`) and the engine publishes
  only on a *change* of error state, never per tick, so a fast failing schedule cannot flood the
  WebSocket.
- **Payload is normalised once**, at configure time, by the same `build_payload` the manual path uses
  (§4) — a payload that could never encode fails in the `PUT`, where the modal shows why, instead of
  logging the identical error forever on a worker thread.
- **Deleting a connection drops its behaviours** (`remove_connection`), and so does *editing* one,
  since `replace()` is delete-then-create (§7). That is intentional: an edit can rename or remove the
  very peer a behaviour targets, and a schedule left pointing at a peer that no longer exists would
  just error forever.

The UI shows them twice on purpose: a badge on the Messages row (the message you configured carries
the evidence) **and** `BehavioursPanel`, which is process-wide. The panel is the load-bearing one — a
schedule keeps firing while you look at a different connection, so a selection-scoped view could show
an idle screen while traffic streams out of a connection one click away.

### 10a. File dialogs work in both shells, and light mode is one CSS block

**Picking files.** The native OS dialogs in `gsim/__main__.py` only exist under PyWebView. In
`--server` mode the UI is an ordinary browser tab, and a `<input type="file">` is no substitute:
browsers deliberately withhold the real path, which is the only thing core can use (a `Structures`
entry goes to `tools.general.resolve_module_name`) or the server can write to. So
`gsim/api/routes/files.py` browses on the server -- which is on the user's own machine anyway -- and
`FilePickerModal.jsx` renders it. Native dialog when `window.pywebview` exists, in-app picker
otherwise; both open at the same roots (`IRS/Structures`, `configs/GsimConfig`). The Browse button is
now always offered -- it used to hide itself without pywebview, which read as the feature vanishing.

The browser-download fallback for Save is gone deliberately: a download lands wherever the browser
puts downloads under a name the user cannot choose, which was the complaint. Both shells now write a
chosen path.

**Loading replaces.** A load reproduces what the file describes, so `importConnections` deletes
every existing connection first (after one confirm, since this closes live links). Merging produced a
session matching neither the file nor what was there, and reused ports failed entries for reasons the
file could not explain.

**Light mode.** Tailwind v4 compiles solid colours to `var(--color-<name>)`, so the whole theme is
the `:root[data-theme='light']` block in `styles.css` -- no component knows a second theme exists.
The slate ramp is *inverted* (950 ground -> lightest, 100 text -> darkest) because the UI uses it
positionally, so every surface relationship survives. Accents are *darkened*, not inverted: sky-400
on near-black is fine, on white it is not. The two dim steps (`slate-600/500`) are measured against
white rather than mirrored -- a straight inversion put 600 at 2.56:1, unreadable for timestamps.
Everything now clears 4.39:1, with sent (7.56) and received (7.68) still distinct hues.

### 11a. `ContextMenu`'s dismissal is containment-checked, not capture-raced

The dismiss listener is `window.addEventListener('mousedown', dismissIfOutside, true)`, and it
checks `menuRef.current?.contains(event.target)` before closing. An earlier version closed
unconditionally on capture and relied on the menu's own `onMouseDown={stopPropagation}` to save a
click on an item -- which does not work: capture-phase listeners on `window` run top-down, before
the event reaches the target, so a bubble-phase `stopPropagation` on the menu is always too late to
stop them. That ordering silently ate every menu click in production while passing every automated
check, because a synthetic `element.click()` fires `click` directly and skips `mousedown` entirely --
the one event the bug lived in. If a UI action can be scripted with `.click()`, that is a hint it
did not exercise the real `mousedown → mouseup → click` sequence; verifying a fix here needs an
actual simulated click (or two real events in order), not a call to `.click()`.

### 11. Right-click menus

`ContextMenu.jsx` + `useContextMenu()` is generic: callers pass an `items` array
(`{label, onSelect, icon?, danger?, disabled?, hint?}`, or `{separator: true}`), so adding an option
is adding an object. The Console uses it for "Clear <pane>", the Behaviours panel for "Stop all" /
"Remove all". Positioning is fixed-viewport and flips near the window edges, measured after mount
because the height depends on how many items the caller passed.

**Clearing is server-side** (`DELETE /api/logs/{direction}` -> `runtime.clear_logs`), not a
client-side view filter: those deques are what `GET /api/logs/{direction}` backfills from, so
clearing only the browser's copy would put every entry straight back on the next refresh or socket
reconnect. The `seq` counter is deliberately **not** reset — it orders entries across threads and
connections, and restarting it would make surviving entries in the other pane sort incorrectly
against new ones.

The clicking client clears its own state **from the DELETE response**, and the `logs.cleared`
broadcast is for OTHER clients. Relying on the broadcast alone was a bug: while the socket is down
no event arrives, and the reconnect snapshot carries connections and behaviours but *not* log
history, so the pane kept showing entries the server had already dropped — indistinguishable from
"Clear did nothing". `App.refillLogs()` therefore also runs on every snapshot (i.e. every
reconnect), which is the only thing that re-syncs a console that missed events while disconnected.

### 12. Two console/layout invariants that are easy to break

- **Auto-scroll is keyed on the newest entry's `seq`, never on `entries.length`.** Each pane keeps
  only `LOG_VIEW_LIMIT` rows, so at the cap every arrival evicts one and the length stops changing —
  a length-keyed effect silently stops following at exactly the point following matters. The
  scroller also sets `overflow-anchor: none`: Chrome's scroll anchoring compensates for content
  removed *above* the viewport, so each evicted row dragged the view up by a row while a new row was
  appended below, drifting off the bottom with nothing appearing to move it.
- **Growable arrays are capped** (`lib/schema.js` `maxArrayItems`). A counted array's length travels
  in a *sibling* scalar, so it can never exceed what that field can count — a `UInt8` counter caps it
  at 255, and exceeding it does not raise, it silently truncates on the receiving side. The limit is
  computed in `FieldList`, which is the only level that can see the sibling. Dynamic arrays have no
  counter and are bounded instead by the uint16 `DataLength` in `framing.py`.

### 12a. `handlers_installed` is tracked separately from `running`

`start()` installs the receive callbacks **before** `unit.start()`, because a peer can have data
waiting the instant the transport opens. That ordering means the two states diverge whenever a start
fails partway: the handlers are registered, but `running` is still False. Guarding installation on
`running` therefore re-registered every route on the next attempt, and core correctly refuses that
(`route ... already has an on-receive callback`) — a 500 on exactly the everyday workflow of bringing
up a TCP client before its server and starting it once the server is up. `ConnectionRecord` carries
`handlers_installed` for this, cleared in `stop()` because core's `close()` drops the callbacks.

`index.html` is served `no-store` (`_WebFiles` in `api/app.py`). Vite fingerprints every asset, so
`index.html` is the only stable filename and the only thing naming the current hashes — if the shell
caches it, the app keeps loading the previous build's bundle and a fix that is provably present in
`dist/` simply never appears.

### 14. Packaging: the frozen app and the MSI

`scripts/build_installer.ps1` is three steps that each consume the previous one:
`npm run build` -> `gsim/web/ui_dist`, `PyInstaller GSim.spec` -> `dist/GSim/` (onedir: `GSim.exe`
beside `_internal/`), `wix build installer/GSim.wxs` -> `dist/GSim-<version>-x64.msi`. The MSI is
per-machine (Program Files), with Start Menu + Desktop shortcuts and a real Add/Remove Programs
entry; `<Files Include="...">` harvests the whole PyInstaller output, so adding a dependency needs
no edit to the .wxs.

Four things about the frozen app are load-bearing, and three of them are invisible until it is
actually installed:

- **`GSim.spec`'s `datas` and `gsim/paths.py` are one contract.** Every read-only directory
  `paths.py` resolves under `BUNDLE_ROOT` has to be copied into the bundle at the SAME relative
  path. The old spec put the UI at `_internal/ui_dist` while `app.py` looked in
  `gsim/web/ui_dist` -- the app started, served its API, and opened on a blank page. Edit the two
  files together.
- **`core` ships as DATA as well as being imported.** `bootstrap.py` checks `CORE_ROOT` exists on
  disk and refuses to start otherwise, and structures modules are loaded from real `.py` files by
  path (`tools.general.import_modules`), which the PYZ archive cannot serve.
- **A windowed build has no `sys.stdout`/`sys.stderr`** -- both are `None`. That is not just "no
  logs": uvicorn's logging config names `ext://sys.stderr`, so `dictConfig` raises and the app exits
  with status 1 the instant it is double-clicked, printing nothing anywhere. `__main__._attach_streams`
  points them at `%LOCALAPPDATA%\GSim\logs\gsim.log` before anything else runs, which is also the
  only diagnostic an installed user can send back. Build with `GSIM_BUILD_CONSOLE=1` to get the same
  bundle with a console attached when something still dies at startup.
- **The install directory is read-only.** Program Files is not writable by a non-elevated user, so
  when frozen `STRUCTURES_DIR`/`CONFIGS_DIR` move to `%LOCALAPPDATA%\GSim` and are seeded once from
  the bundled copies (`paths.seed_user_data`, called from `create_app()`). Seeding is all-or-nothing
  per directory and never overwrites: once the user owns it, a later version silently restoring a
  structures file they deleted would be worse than shipping an update they copy in by hand.
  In a checkout none of this applies -- `paths.py` returns exactly the repo paths GSim has always
  used, so `python -m gsim` is unchanged.

`gsim/__init__.py`'s `__version__` is the only place the version is written: the spec generates the
.exe's VERSIONINFO resource from it and the build script passes it to WiX as `ProductVersion`. Keep
it `major.minor.patch` -- MSI upgrade detection ignores a fourth field, so two builds differing only
there would not upgrade each other.

### 13. Compose drafts outlive the form

`ComposeForm` is deliberately remounted per route (`key` includes connection, destination and
opCode) because the schema differs per route and reusing state would render the wrong fields. That
made in-progress edits vanish whenever the user clicked another message or a console entry. The
payload therefore lives in `App`'s `composeDrafts` — a `Map` in a **ref**, not state, since it
changes on every keystroke and nothing renders from it directly. The form restores from it on mount
and mirrors every change back into it.

### 9. Known `core` issues GSim works around without touching `core`

- `tools.file_functions.read_unit_config` is an unimplemented stub (`...`). The `str` form of
  `ConnectionManager.create(name, "SomeConfigName")` therefore does not work. `to_core_config()`
  (ยง6) always builds and passes a **dict**, which is both the only working path today and the one
  that lets the UI construct configs from form input in the first place.
- `core/connections/CLAUDE.md` still carries a stale "known blocker" note claiming
  `validated_opCode`/`validated_unitCode` reject plain ints; they handle ints correctly as of the
  current `core/tools/general.py`. Noted here, left alone there -- it is inside `core/`.
