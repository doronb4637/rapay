# CLAUDE.md -- gsim

GSim (Generic Simulator): FastAPI + WebSocket API and a React/Tailwind desktop UI built on top of `core`. Users manage connections, compose and send messages through a schema-driven form, and watch live traffic. Cross-package rules are in the root `CLAUDE.md`.

## Core Commands

- Desktop app (free loopback port, PyWebView window): `.venv/Scripts/python.exe -m gsim`.
- Headless API (docs at `http://127.0.0.1:8765/docs`): `.venv/Scripts/python.exe -m gsim --server`.
- UI dev server with hot reload (proxies `/api` and `/ws` to :8765, see `gsim/web/vite.config.js`): `cd gsim/web && npm install && npm run dev`.
- Production UI bundle (served at `/` from `gsim/web/ui_dist` when present): `cd gsim/web && npm run build`.
- Windows installer (UI -> PyInstaller onedir -> `dist/GSim-<version>-x64.msi`): `powershell -ExecutionPolicy Bypass -File scripts/build_installer.ps1`. Uses `.venv3.10`.
- `core`'s test suite must stay green: `.venv/Scripts/python.exe -m pytest` from the repo root.
- Isolation check, must print nothing: `grep -rn "^from core\|^import core" gsim --include=*.py | grep -v core_gateway`.
- Core-untouched check: `git status --porcelain core` shows only the authorised changes below; ask before anything new.

## Architecture & Tech Stack

### The one rule: `core/` is never modified
- No edits, monkey-patches or reaching into private attributes (e.g. `ConnectionManager._connections`). If `core` genuinely needs a change, ask the user first.
- Five authorised exceptions so far, none a precedent: per-link namespaced IRS layouts; the global-registry removal (`@Definition`, per-connection `Interface`, `Connection.interface_for`); `core/IRS/_alias.py` plus `tools.general._assert_is_structure`; `fill()` and codec fixes (`core/IRS/{core,fields,bitfields}.py`, `core/tests/test_irs_fill.py`); `tools.general` loading a structures path from the path given.
- All `core` imports live in `gsim/core_gateway/`. `gsim/api/` and `gsim/web/` call only its public functions (`get_runtime()`, `list_messages()`, `message_schema()`, `build_payload()`, ...).
- `core_gateway/bootstrap.py` is imported first and puts the repo root (not `core/`) on `sys.path`. `CORE_ROOT` is only a filesystem path for `CONFIGS_DIR`.
- `gsim/api/models.py` is where GSim may be stricter than `core` (Pydantic contract). It is not a licence to parse `core` internals.

### Stack and layout
- Backend: FastAPI + WebSocket (`gsim/api/`), desktop launcher in `gsim/__main__.py`, path logic in `gsim/paths.py`. Frontend: React 18, Vite 5, Tailwind v4 (CSS-first, no `tailwind.config.js`), lucide-react in `gsim/web/`.
- `core_gateway/` modules: `schema.py` (IRS class -> JSON form schema), `registry.py` (read-only view of a link's IRS `Interface`), `payloads.py` (form payload -> IRS message), `behaviours.py`, `filters.py`, `fieldpath.py`, `delay.py`, `timing.py`, `runtime.py` (connection registry, logs, thread bridge).
- Routes (`gsim/api/routes/`): `connections`, `messages`, `behaviours`, `filters`, `events` (WebSocket), `files`.

### Threading
- `core`'s API is blocking, so every core-touching route is plain `def` (runs in Starlette's threadpool). Only the `events.py` WebSocket handler is `async def`, and it does no core work.
- Inbound callbacks run on executor threads. `runtime.EventBus.publish` is callable from any thread and hops onto each subscriber's loop with `call_soon_threadsafe`.

### Schema and payloads
- `schema.py` introspects `Message._fields_` into `scalar`/`enum`/`struct`/`bitfield`/`array` kinds. Array length decides the kind: `None` is dynamic, a `str` is counted (sibling field), an `int` is fixed.
- `payloads.prepare_message` resolves the route via `registry.resolve_route` against the link's `Interface` (`record.interface_for(unit_name)`, straight from core's connection), applies a small pre-pass, then `from_dict(...).fill()`. `runtime.send()` and `PUT /behaviours` are its only callers. IRS's `fill()` owns defaults; do not reintroduce a second defaults table.
- The pre-pass owns three things: drop blank `""` inputs, pad/truncate fixed arrays to size, and derive counted-array counts from the list (never from the form).
- An enum with no `0` member is left unset (`None`). A non-zero value no member defines still raises. Bitfield enum bits decode to `None` with a deduped warning instead.
- Hand `send_message` a built message, not a dict, so the route lookup and `from_dict` are not repeated.
- `web/src/lib/schema.js` mirrors `fill()` only so forms start populated; the server re-normalises every payload and `fill()` wins on any disagreement.

### Runtime
- `GSimRuntime` is a process-wide singleton. `_records` is GSim's source of truth; `ConnectionManager` is only a factory plus exit-time `shutdown_all()`.
- Receive handlers: one callback per `(peer, opcode)`, closing over the peer's configured name (core's callback has no route context). Behaviour dispatch happens before `_log` and regardless of filters.
- `LogEntry.payload` is always `to_dict()` in both directions. `LogEntry.unit_code` is the code the layout is registered under (ours on send, the peer's on receive); Inspector fetches schemas by it.
- Both console panes are process-wide, ordered by one global `seq`, with a `connection_name` per row. Per-connection scoping would hide half of every exchange.
- Clearing is server-side (`DELETE /api/logs/{direction}`), never resets `seq`, and the clicking client clears from the response. `App.refillLogs()` also runs on every reconnect snapshot.
- A connection's NAME is its identifier: unique (enforced in `runtime.create/replace`), URL-safe per `CONNECTION_NAME_PATTERN`, `import` reserved. Renaming changes identity.
- Edit is delete-then-recreate (`runtime.replace()`), so logs, behaviours and filters do not survive. A rename onto an existing name is refused before anything is torn down.
- `start()` installs handlers before `unit.start()`, so `handlers_installed` is tracked separately from `running` and cleared in `stop()`.
- `unit.start()` can block indefinitely (core retries refused TCP connects forever). `runtime.start()` runs it on a background thread and waits `CONNECT_GRACE_SECONDS`; otherwise `record.connecting` is set and the result arrives as a `connection.state` event.
- `record.start_token` (bumped by `start`, `stop`, `delete`) lets `_finish_start` detect a superseded attempt and close a connection that came up after the user stopped it. Core cannot cancel the retry loop.
- `connecting` is separate from `running`; only the UI lights both and tints `connecting` amber.
- `index.html` is served `no-store` (`_WebFiles` in `api/app.py`) so a stale shell cannot pin old asset hashes.

### API contract
- `ConnectionCreate.structures` is `min_length=1` (core requires it too and checks peer names/codes against it; the API edge gives the modal field-level errors). `_check_consistency` front-runs duplicate peer unit codes and an invalid `side` for the protocol.
- `to_core_config()` always passes a dict to `ConnectionManager.create()`, never a config name string.
- Create/edit modal narrows core on purpose: remote IP hidden for listening endpoints (pinned to `"0.0.0.0"`; Multicast keeps it as the group address), `Structures` always on the peer except multicast, and per-peer echo fields are explicit on `PeerSpec`.
- Save/Load round-trips the whole connection list. Import (`POST /api/connections/import`) bypasses `ConnectionCreate` and passes the raw config to `runtime.create()` so protocol-specific `extra` keys survive. Loading replaces all existing connections after one confirm.
- Native dialogs exist only under PyWebView; `--server` mode uses `api/routes/files.py` plus `FilePickerModal.jsx`. Both open at `paths.structures_start_dir()` / `CONFIGS_DIR`, and a structures file may live anywhere.
- Filters and behaviours are not written to the session file.

### Behaviours
- A behaviour is `trigger` (`immediate`, `on_connect`, `on_received`) x `mode` (`once`, `periodic`). All fire through `GSimRuntime.sender()`, the same path as the Send button. `kind="periodic"` is the legacy spelling of immediate+periodic.
- Do not use core's `Connection.periodic_sending`: it bypasses `runtime._log` (silent Sent pane), encodes once, and is no faster (Windows timer tick).
- Key rules by route AND trigger. Two `immediate` periodic schedules on one route must collide. `PUT` is an upsert on the whole key.
- Active means `enabled AND connection running`, decided only in `BehaviourEngine._sync`. For reactive rules, active means armed and listening.
- A failing tick never kills the schedule. Publish error state only on change, never per tick.
- `runtime.stop()` pauses behaviours before closing the transport. Workers re-seed `actual_hz` themselves; a stopped worker must not touch counters.
- Sub-15 ms timing: pace against absolute deadlines (overruns skip and realign, counted in `missed_ticks`), sleep via `core_gateway/timing.py` only, and timestamp with `timing.wall_time()`, never `time.time()`.
- `runtime.sender()` builds the message once per worker; `payload_version` invalidates the cached `PreparedSend`.
- `on_received` hooks the existing per-route receive callback; `on_connect` hooks `unit.handle_on_connect` and also fires for an already-connected peer (via `is_unit_connected`), since core does not fire retroactively.
- Conditions: one `Condition` type in `fieldpath.py` shared by filters and behaviours. Delays: one daemon heap thread in `delay.py`, never a `Timer` per firing.
- Mappings never mutate the configured payload, skip an absent source, and require the same kind at both ends, checked in `routes/behaviours.py`.
- Deleting or editing a connection drops its behaviours.

### Console and filters
- The console streams every message, batched and never sampled; a rate limit was tried and reverted because it made 1 ms schedules read as 16 ms. Coalescing happens in `api/routes/events.py` and `_coalesce` merges only consecutive message events.
- Received filters are the only permitted loss: user-authored, per-message, counted (`hits`, `dropped`, `logged`, `dropped_by_change`). They run server-side at the top of `_log`, before `next(self._seq)`. A dropped message is gone and does not burn a `seq`.
- A decode failure is never filtered. Rules decide "interesting", then the change trigger decides "new". Clearing the Received log or starting a connection calls `filters.forget()`.
- Filter paths address `to_dict()`'s real shape (enum = member NAME, bitfield = nested dict). Nothing inside an array is addressable; `schema.filter_targets` never descends into arrays.
- Counters publish on a 2 Hz heartbeat. The per-row Hide (local mute) stays separate from server drops. See `gsim/docs/FILTERS.md` for usage.

### Frontend
- `FieldRenderer.jsx` is recursive and composes via `value` + `onChange(next)`, so form state is already the payload dict. Design tokens live only in `components/ui.jsx`.
- Light mode is the single `:root[data-theme='light']` block in `styles.css`. Any accent class at a colour step that block does not define silently falls through to Tailwind's value, so check contrast.
- Console auto-scroll keys on the newest entry's `seq`, never on `entries.length`, and the scroller sets `overflow-anchor: none`.
- Growable arrays are capped by `maxArrayItems` (`lib/schema.js`) to what the sibling counter can hold. Compose drafts live in a ref `Map` in `App` so they survive remounts.
- `ContextMenu` dismisses by containment check, not capture-phase racing. Test UI actions with real `mousedown -> mouseup -> click`, not `element.click()`.

### Packaging (frozen app and MSI)
- `GSim.spec` `datas` and `gsim/paths.py` are one contract: every read-only directory under `BUNDLE_ROOT` must be copied at the same relative path. Edit them together.
- `core` ships as data as well as being imported (`bootstrap.py` checks `CORE_ROOT`; structures load from real `.py` files). Structures files are not shipped, seeded or owned by GSim.
- A windowed build has no `sys.stdout`/`sys.stderr`; `__main__._attach_streams` points them at `%LOCALAPPDATA%\GSim\logs\gsim.log`. Set `GSIM_BUILD_CONSOLE=1` for a console build.
- Program Files is read-only: when frozen, `CONFIGS_DIR` moves to `%LOCALAPPDATA%\GSim` and is seeded once and never overwritten (`paths.seed_user_data`).
- `gsim/__init__.py` `__version__` is the only version source (`major.minor.patch`; MSI ignores a fourth field).

## Code Style & Constraints

- Follow the root conventions. Beyond them: keep GSim stricter than core only in `api/models.py`.
- Facts about `core` behaviour must be verified against real code; re-verify a stale claim against the real encoder before trusting a comment.
- Never add a second copy of `core`'s defaulting rules; call `fill()`.
- Keep route handlers that touch `core` as plain `def`.

## Reference Codebases

- Gateway boundary: `gsim/core_gateway/bootstrap.py`, `gsim/core_gateway/runtime.py`.
- Schema and payload logic: `gsim/core_gateway/schema.py`, `gsim/core_gateway/payloads.py`; client mirrors `gsim/web/src/lib/schema.js`, `gsim/web/src/lib/fieldTargets.js`.
- API contract: `gsim/api/models.py`, `gsim/api/routes/`, `gsim/api/app.py`.
- Behaviours and timing: `gsim/core_gateway/behaviours.py`, `gsim/core_gateway/timing.py`, `gsim/core_gateway/delay.py`.
- Filters: `gsim/core_gateway/filters.py`, `gsim/docs/FILTERS.md`.
- UI primitives and form renderer: `gsim/web/src/components/ui.jsx`, `gsim/web/src/components/FieldRenderer.jsx`.
- Packaging: `GSim.spec`, `gsim/paths.py`, `installer/GSim.wxs`, `scripts/build_installer.ps1`.
