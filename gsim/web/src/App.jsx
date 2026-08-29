/**
 * GSim dashboard shell.
 *
 *   ┌────────────────────────────────────────────────────────────┐
 *   │ title bar                                                  │
 *   ├──────────┬───────────────────────────────┬─────────────────┤
 *   │ Sidebar  │ Messages (workspace)          │ Console         │
 *   │ conns    ├───────────────────────────────┤ sent + received │
 *   │          │ Inspector (compose | inspect) │                 │
 *   └──────────┴───────────────────────────────┴─────────────────┘
 *
 * Live log and state updates arrive over one WebSocket rather than by polling,
 * because inbound messages originate on core's executor threads and are pushed
 * the moment they land.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Download, Moon, Sun, Upload } from 'lucide-react';
import { api, openEventStream } from './api';
import { buildSessionExport, parseSessionImport } from './lib/sessionFile';
import { applyTheme, initialTheme } from './lib/theme';
import { clamp, readPref, writePref } from './lib/prefs';
import FilePickerModal from './components/FilePickerModal';
import Logo from './components/Logo';
import Sidebar from './components/Sidebar';
import MessagesTable from './components/MessagesTable';
import Inspector from './components/Inspector';
import LinkOverview from './components/LinkOverview';
import Console from './components/Console';
import ConnectionModal from './components/ConnectionModal';
import BehaviourModal from './components/BehaviourModal';
import BehavioursPanel from './components/BehavioursPanel';
import FilterModal from './components/FilterModal';
import Resizer from './components/Resizer';
import { Button, IconButton, StatusDot, cx } from './components/ui';

//: Only present inside the pywebview desktop shell -- checked at click time
//: (not cached in state) since by then the window has certainly finished
//: loading, unlike the modal's Browse button which can render before
//: pywebview's ready event fires.
const canUseNativeFiles = () =>
  typeof window !== 'undefined' && !!window.pywebview?.api?.save_config_file;

//: How many entries each console pane keeps on screen. The server holds more
//: (`LOG_LIMIT` in core_gateway/runtime.py); this is purely how much scrollback
//: the UI renders. Applied to the backfill as well as the live stream, so a
//: reconnect cannot quietly hand the pane thousands of rows.
const LOG_VIEW_LIMIT = 30;

//: Both rails are draggable and remembered. The bounds are what keeps a stored
//: value from a wider monitor -- or an over-enthusiastic drag -- from squeezing
//: the workspace out of existence; `clamp` is applied on read as well as on
//: change, so a stale preference cannot strand a panel off screen.
const RAIL = { min: 208, max: 420, initial: 256 };
const CONSOLE = { min: 300, max: 720, initial: 432 };

export default function App() {
  const [connections, setConnections] = useState([]);
  const [selectedName, setSelectedName] = useState(null);
  const [sent, setSent] = useState([]);
  const [received, setReceived] = useState([]);
  const [selectedLog, setSelectedLog] = useState(null);

  // A fast periodic behaviour (interval well under a frame, e.g. 0.01s) can
  // push 'message.sent'/'message.received' events faster than the browser can
  // paint. Applying each one as its own setState re-renders the console --
  // and re-runs its auto-scroll effect -- at that same rate, so the row under
  // the pointer keeps sliding away between mousedown and click and a click
  // that looks well-aimed lands on nothing. Coalescing everything that arrives
  // within one animation frame into a single state update caps the console's
  // re-render/scroll rate at the display's, independent of how fast the
  // socket is actually delivering -- no message is dropped, only how often the
  // DOM reacts to them.
  const pendingLogsRef = useRef({ sent: [], received: [] });
  const flushHandleRef = useRef(null);
  const flushPendingLogs = () => {
    flushHandleRef.current = null;
    const { sent: pendingSent, received: pendingReceived } = pendingLogsRef.current;
    pendingLogsRef.current = { sent: [], received: [] };
    if (pendingSent.length) {
      setSent((current) => [...current, ...pendingSent].slice(-LOG_VIEW_LIMIT));
    }
    if (pendingReceived.length) {
      setReceived((current) => [...current, ...pendingReceived].slice(-LOG_VIEW_LIMIT));
    }
  };
  const queueLogEntry = (direction, entry) => {
    const pending = pendingLogsRef.current[direction];
    pending.push(entry);
    // Never hold more than the pane can show. `requestAnimationFrame` does not
    // fire at all while the window is minimised or fully occluded -- so without
    // this the buffer grows at the FULL send rate for as long as that lasts,
    // which under a 1kHz behaviour is ~2000 entries (~5MB) a second, retained,
    // for a view that will only ever render the last LOG_VIEW_LIMIT of them.
    // Trimming here costs nothing: `flushPendingLogs` slices to the same cap.
    if (pending.length > LOG_VIEW_LIMIT) {
      pending.splice(0, pending.length - LOG_VIEW_LIMIT);
    }
    if (flushHandleRef.current === null) {
      flushHandleRef.current = requestAnimationFrame(flushPendingLogs);
    }
  };
  // A clear/backfill/removal replaces `sent`/`received` outright -- anything
  // still queued from before it must not survive to be appended afterwards.
  const discardPendingLogs = (direction = null) => {
    if (direction) pendingLogsRef.current[direction] = [];
    else pendingLogsRef.current = { sent: [], received: [] };
  };
  useEffect(() => {
    return () => {
      if (flushHandleRef.current !== null) cancelAnimationFrame(flushHandleRef.current);
    };
  }, []);
  const [selection, setSelection] = useState(null);   // Inspector mode
  const [modal, setModal] = useState(null);           // null | {} | connection
  const [online, setOnline] = useState(false);
  // The WebSocket specifically, tracked apart from `online` (which only says
  // the HTTP API answered). The two are genuinely independent: a dropped feed
  // leaves every button working while nothing pushed ever arrives again, and
  // without its own indicator that reads as the UI ignoring you.
  const [liveFeed, setLiveFeed] = useState(false);
  const [toast, setToast] = useState(null);
  // Which peer we are composing TO. Chosen before the message list, because
  // each link has its own IRS: the set of messages this connection can send
  // genuinely differs per destination, so a single union list would offer rows
  // that fail on send.
  const [destination, setDestination] = useState(null);
  const [behaviours, setBehaviours] = useState([]);
  // Received filters. Process-wide like behaviours, and for the same reason: a
  // filter keeps dropping messages while you look at a different connection.
  const [filters, setFilters] = useState([]);
  const [filtersOpen, setFiltersOpen] = useState(false);
  // null | {opCode, payload, messageName, connectionName, unitName}
  const [behaviourDraft, setBehaviourDraft] = useState(null);
  // In-progress compose payloads, keyed by route. ComposeForm is deliberately
  // remounted per route (the schema differs), so its own state cannot survive
  // clicking another message or a log entry -- half-filled work vanished. The
  // draft lives here instead, above every unmount, and the form restores from
  // it. Kept in a ref, not state: it changes on every keystroke and nothing
  // renders from it directly, so re-rendering the whole app per character
  // would be pure waste.
  const composeDrafts = useRef(new Map());
  // null | {mode:'open'|'save', contents?} -- the in-app picker for config
  // Save/Load, used when no native dialog exists (`--server` + browser tab).
  const [configPicker, setConfigPicker] = useState(null);
  const [theme, setTheme] = useState(initialTheme);
  // Panel widths, and the file this session was last saved to or loaded from --
  // the one durable fact about a session that the title bar had nothing to say
  // about while it sat 900px empty.
  const [railWidth, setRailWidth] = useState(() =>
    clamp(readPref('rail.width', RAIL.initial), RAIL.min, RAIL.max));
  const [consoleWidth, setConsoleWidth] = useState(() =>
    clamp(readPref('console.width', CONSOLE.initial), CONSOLE.min, CONSOLE.max));
  const [sessionFile, setSessionFile] = useState(null);

  useEffect(() => { applyTheme(theme); }, [theme]);

  // `null` means "reset", which is what double-clicking a separator sends.
  const resizeRail = (next) => {
    const value = next === null ? RAIL.initial : clamp(next, RAIL.min, RAIL.max);
    setRailWidth(value);
    writePref('rail.width', value);
  };
  const resizeConsole = (next) => {
    const value = next === null ? CONSOLE.initial : clamp(next, CONSOLE.min, CONSOLE.max);
    setConsoleWidth(value);
    writePref('console.width', value);
  };

  const selected = connections.find((c) => c.name === selectedName) ?? null;
  const peers = selected?.peers ?? [];
  // Exact counts, not samples. `active` (really firing) rather than `enabled`
  // (intended to), for the same reason the Behaviours panel uses it: a schedule
  // armed on a stopped connection is not traffic.
  const runningCount = connections.filter((connection) => connection.running).length;
  const firingCount = behaviours.filter((behaviour) => behaviour.active).length;

  const refresh = useCallback(async () => {
    try {
      setConnections(await api.listConnections());
      setOnline(true);
    } catch {
      setOnline(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Backfill the console across every connection -- not per selection. A send
  // is logged against the sender and its matching receive against the
  // recipient, so scoping the console to the selected connection would hide
  // one half of every exchange (that was the "no received messages" bug).
  //
  // Called again on every WebSocket (re)connect, because the socket is the only
  // thing that keeps these lists current: while it is down no `message.*` or
  // `logs.cleared` event arrives, and the reconnect snapshot carries
  // connections and behaviours but not log history. Without this re-sync a
  // dropped socket leaves the console permanently stale -- showing entries the
  // server has already cleared, which is exactly how "Clear did nothing" looks.
  const refillLogs = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([api.allLogs('sent'), api.allLogs('received')]);
      setSent(s.slice(-LOG_VIEW_LIMIT));
      setReceived(r.slice(-LOG_VIEW_LIMIT));
    } catch {
      /* offline; the next reconnect retries */
    }
  }, []);

  useEffect(() => {
    refillLogs();
    api.behaviours().then(setBehaviours, () => {});
    api.filters().then(setFilters, () => {});
  }, [refillLogs]);

  // Changing connection only re-aims the Inspector; the console keeps streaming.
  useEffect(() => {
    setSelectedLog(null);
    setSelection(null);
  }, [selectedName]);

  // Default to the first peer, and re-aim if the chosen one disappears (an
  // edit can rename or remove peers under us).
  useEffect(() => {
    if (!peers.some((peer) => peer.name === destination)) {
      setDestination(peers[0]?.name ?? null);
    }
  }, [peers, destination]);

  // The message list is per-link, so switching destination invalidates
  // whatever compose form is open against the old one.
  useEffect(() => {
    setSelection((current) => (current?.mode === 'compose' ? null : current));
  }, [destination]);

  useEffect(
    () =>
      openEventStream((event) => {
        setOnline(true);
        if (event.type === 'snapshot') {
          setConnections(event.connections);
          // Behaviours ride the snapshot: they keep firing across a dropped
          // socket, so a reconnect must not leave the panel showing none.
          if (event.behaviours) setBehaviours(event.behaviours);
          // Filters ride the snapshot too: they keep dropping across a dropped
          // socket, so a reconnect without them would show a quiet Received
          // pane with nothing on screen saying why.
          if (event.filters) setFilters(event.filters);
          // The snapshot does NOT carry log history, and everything that
          // happened while the socket was down was missed -- re-sync it.
          discardPendingLogs();
          refillLogs();
          return;
        }
        // The server sends the whole list on any change, so this is a replace,
        // not a merge -- a dropped event cannot leave a stale schedule on screen.
        if (event.type === 'behaviours') return setBehaviours(event.behaviours);
        if (event.type === 'filters') return setFilters(event.filters);
        if (event.type === 'logs.cleared') {
          const drop = () => [];
          discardPendingLogs(event.direction);
          if (event.direction === 'sent') setSent(drop);
          else setReceived(drop);
          setSelectedLog((current) =>
            current?.direction === event.direction ? null : current,
          );
          setSelection((current) =>
            current?.mode === 'inspect' && current.entry.direction === event.direction
              ? null
              : current,
          );
          return;
        }
        if (event.type === 'connection.state') return refresh();
        if (event.type === 'connection.deleted') {
          // Drop the departed connection's history so the console does not
          // keep referring to something the user just removed.
          const gone = event.connection_name;
          const drop = (list) => list.filter((entry) => entry.connection_name !== gone);
          pendingLogsRef.current.sent = pendingLogsRef.current.sent.filter(
            (entry) => entry.connection_name !== gone,
          );
          pendingLogsRef.current.received = pendingLogsRef.current.received.filter(
            (entry) => entry.connection_name !== gone,
          );
          setSent(drop);
          setReceived(drop);
          return refresh();
        }
        // One frame, N entries. The server coalesces whatever piled up while it
        // was writing the previous frame (see api/routes/events.py), so under a
        // fast behaviour this arrives as batches rather than as a thousand
        // separate socket messages a second -- but it carries EVERY entry, so
        // the timestamps the console renders are the real send times. The
        // singular form is still handled: it is what a lone event looks like
        // when nothing had to be batched.
        if (event.type === 'messages') {
          event.entries.forEach((entry) =>
            queueLogEntry(entry.direction === 'sent' ? 'sent' : 'received', entry));
          return;
        }
        if (event.type === 'message.sent' || event.type === 'message.received') {
          queueLogEntry(event.entry.direction === 'sent' ? 'sent' : 'received', event.entry);
        }
      }, setLiveFeed),
    [refresh, refillLogs],
  );

  const notify = (message) => {
    setToast(message);
    setTimeout(() => setToast(null), 3200);
  };

  const guard = async (action) => {
    try {
      await action();
    } catch (err) {
      notify(err.message);
    }
  };

  // Loaded sequentially, not in parallel: predictable ordering for the
  // failure message, and no risk of racing two imports that happen to name
  // the same port. `api.importConnection` is core-shaped input (see
  // lib/sessionFile.js) -- each entry is exactly one `GET /api/connections`
  // record's `{name, config}`, re-submitted via `POST /api/connections/import`.
  const importConnections = async (entries) => {
    // A load reproduces what the FILE describes, so anything already open is
    // replaced rather than merged onto -- merging silently produced a session
    // that matched neither the file nor what was there before, and reused
    // ports failed entries for reasons the file could not explain. Confirmed
    // first because this closes live connections.
    const existing = connections.length;
    if (existing) {
      const ok = window.confirm(
        `Replace ${existing} open connection${existing === 1 ? '' : 's'} with ` +
        `${entries.length} from this file?\n\nRunning connections will be closed.`,
      );
      if (!ok) return;
      for (const connection of connections) {
        try {
          await api.deleteConnection(connection.name);
        } catch {
          /* already gone; the reload below reflects the truth either way */
        }
      }
      setSelectedName(null);
      await refresh();
    }

    let succeeded = 0;
    let firstError = null;
    for (const entry of entries) {
      try {
        await api.importConnection(entry);
        succeeded += 1;
      } catch (err) {
        firstError ??= err;
      }
    }
    await refresh();
    if (firstError) {
      throw new Error(
        `Loaded ${succeeded}/${entries.length} connection${entries.length === 1 ? '' : 's'}. ` +
        `First failure: ${firstError.message}`,
      );
    }
    notify(`Loaded ${succeeded} connection${succeeded === 1 ? '' : 's'}.`);
  };

  // Desktop: the OS dialogs in `gsim/__main__.py`, which open at
  // configs/GsimConfig. Browser (`--server`): the in-app picker backed by
  // `/api/files/*`, which opens at the same directory and writes through the
  // server -- a download would land wherever the browser puts downloads, with
  // no say in the name or the folder.
  //: Just the file name out of a full path, for the title bar. The whole path
  //: is too long to stand there and the folder never varies within a session.
  const baseName = (path) => String(path).split(/[\\/]/).filter(Boolean).pop() ?? null;

  const handleSaveConfig = () =>
    guard(async () => {
      const text = JSON.stringify(buildSessionExport(connections), null, 2);
      if (canUseNativeFiles()) {
        const path = await window.pywebview.api.save_config_file(text);
        // The desktop bridge returns the chosen path, or nothing if the dialog
        // was cancelled -- so a cancel must not relabel the title bar.
        if (path) setSessionFile(baseName(path));
      } else {
        setConfigPicker({ mode: 'save', contents: text });
      }
    });

  const handleLoadConfigClick = () => {
    if (!canUseNativeFiles()) return setConfigPicker({ mode: 'open' });
    guard(async () => {
      const text = await window.pywebview.api.load_config_file();
      if (text) await importConnections(parseSessionImport(text));
    });
  };

  const pickLog = (entry) => {
    setSelectedLog(entry);
    setSelection({ mode: 'inspect', entry });
  };

  /** The behaviour on the route currently open in Compose, if any. */
  const composeBehaviour =
    selection?.mode === 'compose'
      ? behaviours.find(
          (behaviour) =>
            behaviour.connection_name === selectedName &&
            behaviour.unit_name === destination &&
            behaviour.op_code === selection.opCode,
        )
      : undefined;

  /** Open the dialog from a Behaviours-panel row: jump the app to that route
   *  first, so saving writes back to the behaviour you clicked rather than to
   *  whatever happens to be selected. */
  const editBehaviour = (behaviour) => {
    setSelectedName(behaviour.connection_name);
    setDestination(behaviour.unit_name);
    setBehaviourDraft({
      connectionName: behaviour.connection_name,
      unitName: behaviour.unit_name,
      opCode: behaviour.op_code,
      messageName: behaviour.message_name,
      // The rule's own id, so the dialog edits THIS one. A route can hold
      // several behaviours now (one per trigger), and matching on the route
      // alone would open whichever happened to be found first.
      id: behaviour.id,
      // Reuse the stored payload: the compose form is not necessarily showing
      // this route, so its current state is the wrong thing to save.
      payload: behaviour.payload,
    });
  };

  const behaviourActions = {
    onStart: (id) => api.startBehaviour(id),
    onStop: (id) => api.stopBehaviour(id),
    onDelete: (id) => api.deleteBehaviour(id),
  };

  return (
    <div className="flex h-full flex-col overflow-hidden bg-slate-950">
      <header className="flex h-11 shrink-0 items-center gap-2 border-b border-slate-800 bg-slate-900 px-3">
        <div className="flex shrink-0 items-center gap-2">
          <Logo size={22} />
          <span className="text-[13px] font-semibold tracking-tight text-slate-100">GSim</span>
        </div>

        {/* Save/Load the whole session as one JSON file -- lives here, not in
            the Connections panel header, because that header is too narrow to
            reliably show both buttons without one landing under the Inspector
            layout next to it (the original bug report).

            LABELLED, not bare icons. Loading replaces every open connection and
            closes anything running, which made these the two most consequential
            controls in the app and, as unlabelled glyphs behind tooltips, the
            two least legible. Rare and destructive is exactly the pair that
            should read as words. */}
        <span className="mx-1 h-4 w-px shrink-0 bg-slate-800" />
        <Button
          icon={Download}
          title={connections.length ? 'Save connections to a file' : 'No connections to save'}
          disabled={!connections.length}
          onClick={handleSaveConfig}
        >
          Save
        </Button>
        <Button icon={Upload} title="Load connections from a file" onClick={handleLoadConfigClick}>
          Load
        </Button>

        {/* The middle used to be ~900px of nothing until a failure chip
            appeared. It now stands for the session: the file it came from and
            what is actually moving. Both are counted exactly rather than
            sampled, so neither can overstate itself. */}
        <div className="mx-2 flex min-w-0 flex-1 items-baseline gap-3 overflow-hidden">
          {sessionFile && (
            <span className="truncate font-mono text-[11px] text-slate-400" title={sessionFile}>
              {sessionFile}
            </span>
          )}
          {connections.length > 0 && (
            <span className="shrink-0 whitespace-nowrap text-[10px] text-slate-500">
              <span className="tnum text-slate-400">{runningCount}</span>
              {` of ${connections.length} running`}
              {firingCount > 0 && (
                <>
                  <span className="text-slate-700"> · </span>
                  <span className="tnum text-emerald-400">{firingCount}</span>
                  {firingCount === 1 ? ' behaviour firing' : ' behaviours firing'}
                </>
              )}
            </span>
          )}
        </div>

        {/* Only surface these when something is actually DOWN -- a permanent
            "connected" chip is noise that says nothing the rest of the UI
            is not already showing.

            The live feed gets its own indicator because losing it is silent
            otherwise: every button still works over HTTP, so the app looks
            healthy while no incoming message, connection-state change or
            clear broadcast ever lands again. */}
        {!online ? (
          <span className="flex shrink-0 items-center gap-1.5 text-[10px] font-medium text-rose-400">
            <StatusDot on={false} className="h-1.5 w-1.5 !bg-rose-500" />
            API unreachable
          </span>
        ) : (
          !liveFeed && (
            <span
              className="flex shrink-0 items-center gap-1.5 text-[10px] font-medium text-amber-400"
              title="The WebSocket feed is down, so nothing pushed by the server will appear until it reconnects. Actions you take still work."
            >
              <StatusDot on={false} className="h-1.5 w-1.5 !bg-amber-500" />
              Live feed reconnecting…
            </span>
          )
        )}

        <span className="mx-1 h-4 w-px shrink-0 bg-slate-800" />
        <IconButton
          icon={theme === 'light' ? Moon : Sun}
          title={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
          onClick={() => setTheme((current) => (current === 'light' ? 'dark' : 'light'))}
        />
      </header>

      <div className="flex min-h-0 flex-1">
        {/* Left column: Connections above, the selected connection's Messages
            below -- browsing a connection and its messages is one continuous
            gesture, so they belong in one column. */}
        <div
          className="flex shrink-0 flex-col"
          style={{ width: `${railWidth}px` }}
        >
          {/* Messages gets ~43% more height than Connections (1 : 1.43). */}
          <Sidebar
            className="min-h-0 flex-[1] border-b border-slate-800"
            connections={connections}
            selectedName={selectedName}
            onSelect={setSelectedName}
            onCreate={() => setModal({})}
            onEdit={(connection) => setModal(connection)}
            onDelete={(connection) =>
              guard(async () => {
                await api.deleteConnection(connection.name);
                if (connection.name === selectedName) setSelectedName(null);
                await refresh();
              })
            }
            onToggle={(connection) =>
              guard(async () => {
                // `connecting` (still inside `unit.start()`, retrying a
                // refused connect -- core/connections/base.py) reads as "on"
                // for this purpose same as `running` does: the click has to
                // mean Stop, not a second Start that the backend would just
                // no-op (`runtime.start()` returns early while already
                // `connecting`).
                const on = connection.running || connection.connecting;
                await (on ? api.stop(connection.name) : api.start(connection.name));
                await refresh();
              })
            }
          />
          <MessagesTable
            className="min-h-0 flex-[1.43]"
            connectionName={selectedName}
            peers={peers}
            destination={destination}
            onDestinationChange={setDestination}
            activeOpCode={selection?.mode === 'compose' ? selection.opCode : null}
            behaviours={behaviours}
            onCompose={(opCode) => {
              setSelectedLog(null);
              setSelection({ mode: 'compose', opCode });
            }}
          />
          {/* Only once something is scheduled -- costs the layout nothing
              until it has something to report. Bounded height so a long list
              scrolls rather than squeezing Messages out of the column. */}
          {behaviours.length > 0 && (
            <BehavioursPanel
              className="max-h-52 shrink-0 border-t border-slate-800"
              behaviours={behaviours}
              connections={connections}
              onEdit={editBehaviour}
              onStart={(id) => guard(() => behaviourActions.onStart(id))}
              onStop={(id) => guard(() => behaviourActions.onStop(id))}
              onDelete={(id) => guard(() => behaviourActions.onDelete(id))}
            />
          )}
        </div>

        <Resizer
          value={railWidth}
          min={RAIL.min}
          max={RAIL.max}
          onChange={resizeRail}
          side="left"
          label="Connections panel width"
        />

        <main className="flex min-w-0 flex-1 flex-col">
          <Inspector
            connectionName={selectedName}
            selection={selection}
            peers={peers}
            destination={destination}
            onSent={() => {}}
            draftKey={`${selectedName}:${destination}:${selection?.opCode}`}
            drafts={composeDrafts.current}
            behaviour={composeBehaviour}
            onBehaviour={({ opCode, payload, messageName }) =>
              setBehaviourDraft({
                connectionName: selectedName,
                unitName: destination,
                opCode,
                payload,
                messageName,
              })
            }
          >
            {/* The resting state of the app's largest panel -- what it shows
                when nothing is selected, which is how the app starts and where
                it returns after every clear. Passed in rather than fetched
                inside the Inspector because everything it draws is state App
                already holds. */}
            <LinkOverview
              connection={selected}
              sent={sent}
              received={received}
              onPickPeer={(peerName) => {
                setDestination(peerName);
                setSelection(null);
              }}
            />
          </Inspector>
        </main>

        <Resizer
          value={consoleWidth}
          min={CONSOLE.min}
          max={CONSOLE.max}
          onChange={resizeConsole}
          side="right"
          label="Console width"
        />

        <Console
          style={{ width: `${consoleWidth}px` }}
          sent={sent}
          received={received}
          selected={selectedLog}
          onSelect={pickLog}
          onClear={(direction) =>
            guard(async () => {
              await api.clearLogs(direction);
              // Then RE-READ both panes from the server rather than assuming
              // what the result should be. Two reasons this is not paranoia:
              // the `logs.cleared` broadcast only reaches other clients (and
              // not at all while this socket is down), and an optimistic local
              // reset is a guess about state the server actually owns -- if the
              // two ever disagree the pane keeps showing entries that no longer
              // exist, with nothing to correct it. Asking is one request and
              // cannot drift.
              await refillLogs();
              setSelectedLog((current) =>
                current?.direction === direction ? null : current,
              );
              setSelection((current) =>
                current?.mode === 'inspect' && current.entry.direction === direction
                  ? null
                  : current,
              );
            })
          }
          filters={filters}
          onOpenFilters={() => setFiltersOpen(true)}
        />
      </div>

      {modal && (
        <ConnectionModal
          initial={modal.name ? toForm(modal) : null}
          onSubmit={async (body) => {
            const wasSelected = modal.name && modal.name === selectedName;
            const record = modal.name
              ? await api.updateConnection(modal.name, body)
              : await api.createConnection(body);
            await refresh();
            // The name IS the identity, so an edit that renames moves the
            // connection to a new one. Follow it, or the selection would point
            // at a name that no longer exists and every panel would blank out.
            if (wasSelected && record?.name && record.name !== modal.name) {
              setSelectedName(record.name);
            }
            // The connection was created; it just could not dial out yet (a TCP
            // client whose server is not listening). The modal closes either
            // way -- this only explains the stopped status dot.
            if (record?.start_error) {
              notify(`"${record.name}" created but not started: ${record.start_error}`);
            }
          }}
          onClose={() => setModal(null)}
        />
      )}

      {behaviourDraft && (
        <BehaviourModal
          messageName={behaviourDraft.messageName}
          opCode={behaviourDraft.opCode}
          destination={behaviourDraft.unitName}
          connectionName={behaviourDraft.connectionName}
          peers={peers}
          // A route can now hold several rules -- one per trigger -- so opening
          // the dialog from a Messages row edits the one it was opened for, and
          // otherwise starts a new one. `behaviourDraft.id` is set when the
          // Behaviours panel opens an existing rule; a Messages row has no id
          // and falls back to the route's `immediate` rule, which is the one
          // that badge has always meant.
          existing={
            behaviourDraft.id
              ? behaviours.find((behaviour) => behaviour.id === behaviourDraft.id)
              : behaviours.find(
                  (behaviour) =>
                    behaviour.connection_name === behaviourDraft.connectionName &&
                    behaviour.unit_name === behaviourDraft.unitName &&
                    behaviour.op_code === behaviourDraft.opCode &&
                    behaviour.trigger === 'immediate',
                )
          }
          onSubmit={(rule) =>
            api.setBehaviour(behaviourDraft.connectionName, {
              unit_name: behaviourDraft.unitName,
              op_code: behaviourDraft.opCode,
              payload: behaviourDraft.payload,
              ...rule,
            })
          }
          {...behaviourActions}
          onClose={() => setBehaviourDraft(null)}
        />
      )}

      {/* Received filters. Opened from the Received pane, and scoped to the
          selected connection by default -- the dialog can switch, but starting
          somewhere other than where the user already is would be a puzzle.
          Every action re-reads from the server response rather than mutating
          local state, for the same reason clearing the console does: the
          `filters` broadcast is for OTHER clients, and while this socket is
          down none arrives. */}
      {filtersOpen && (
        <FilterModal
          connections={connections}
          filters={filters}
          initialConnection={selectedName}
          onSave={async (connectionName, body) => {
            await api.setFilter(connectionName, body);
            setFilters(await api.filters());
          }}
          onDelete={async (id) => {
            await api.deleteFilter(id);
            setFilters(await api.filters());
          }}
          onArm={async (id) => {
            await api.armFilter(id);
            setFilters(await api.filters());
          }}
          onDisarm={async (id) => {
            await api.disarmFilter(id);
            setFilters(await api.filters());
          }}
          onDisarmAll={async () => setFilters(await api.disarmAllFilters())}
          onClose={() => setFiltersOpen(false)}
        />
      )}

      {/* Browser-mode Save/Load. `suffix` is what keeps the listing to configs
          rather than every file in the folder. */}
      {configPicker && (
        <FilePickerModal
          mode={configPicker.mode}
          title={configPicker.mode === 'save' ? 'Save connections' : 'Load connections'}
          suffix=".json"
          defaultFileName="gsim-connections.json"
          onPick={async (path) => {
            if (configPicker.mode === 'save') {
              await api.saveFile(path, configPicker.contents);
              setSessionFile(baseName(path));
              notify(`Saved to ${path}`);
            } else {
              const { contents } = await api.readFile(path);
              await importConnections(parseSessionImport(contents));
              setSessionFile(baseName(path));
            }
          }}
          onClose={() => setConfigPicker(null)}
        />
      )}

      {toast && (
        <div
          className={cx(
            'fixed bottom-4 left-1/2 z-50 -translate-x-1/2',
            'max-w-lg rounded-lg border border-rose-900/70 bg-rose-950/90 px-3 py-2',
            'font-mono text-[11px] text-rose-200 shadow-lg backdrop-blur',
          )}
        >
          {toast}
        </div>
      )}
    </div>
  );
}

/** Connection record -> modal form shape (for Edit). */
function toForm(connection) {
  const config = connection.config ?? {};
  const entries = Object.entries(config.connections ?? {});
  // Single-peer, non-multicast connections used to keep their Structures at
  // the connection level (the modal's top-level section, before it moved into
  // each peer's own row). A config saved back then still has it there --
  // backfill it onto the one peer so Edit shows it in its now-permanent home
  // instead of an empty field.
  const singlePeerLegacyStructures =
    entries.length === 1 && config.protocol !== 'multicast' ? config.Structures : null;

  return {
    name: connection.name,
    protocol: config.protocol,
    side: config.side,
    ip: config.ip,
    local_ip: config.local_ip,
    unitCode: config.unitCode,
    peers: entries.map(([name, spec]) => ({
      name, port: spec.port, unitCode: spec.unitCode,
      // Each link carries its own layouts; core writes the canonical spelling.
      structures: spec.Structures ?? spec.structures ?? singlePeerLegacyStructures ?? [''],
      // Per-peer echo override, same canonical spelling `to_core_config` writes.
      echo_opcode: spec.echo_opcode ?? '',
      echo_interval: spec.EchoInterval ?? '',
      echo_timeout: spec.EchoTimeout ?? '',
    })),
    structures: config.Structures ?? [''],
    echo_opcode: config.echo_opcode ?? '',
    echo_interval: config.EchoInterval ?? '',
    echo_timeout: config.EchoTimeout ?? '',
  };
}
