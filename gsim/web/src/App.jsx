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
import FilePickerModal from './components/FilePickerModal';
import Logo from './components/Logo';
import Sidebar from './components/Sidebar';
import MessagesTable from './components/MessagesTable';
import Inspector from './components/Inspector';
import Console from './components/Console';
import ConnectionModal from './components/ConnectionModal';
import BehaviourModal from './components/BehaviourModal';
import BehavioursPanel from './components/BehavioursPanel';
import { IconButton, StatusDot, cx } from './components/ui';

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

export default function App() {
  const [connections, setConnections] = useState([]);
  const [selectedName, setSelectedName] = useState(null);
  const [sent, setSent] = useState([]);
  const [received, setReceived] = useState([]);
  const [selectedLog, setSelectedLog] = useState(null);
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

  useEffect(() => { applyTheme(theme); }, [theme]);

  const selected = connections.find((c) => c.name === selectedName) ?? null;
  const peers = selected?.peers ?? [];

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
          // The snapshot does NOT carry log history, and everything that
          // happened while the socket was down was missed -- re-sync it.
          refillLogs();
          return;
        }
        // The server sends the whole list on any change, so this is a replace,
        // not a merge -- a dropped event cannot leave a stale schedule on screen.
        if (event.type === 'behaviours') return setBehaviours(event.behaviours);
        if (event.type === 'logs.cleared') {
          const drop = () => [];
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
          setSent(drop);
          setReceived(drop);
          return refresh();
        }
        if (event.type === 'message.sent' || event.type === 'message.received') {
          const push = (list) => [...list, event.entry].slice(-LOG_VIEW_LIMIT);
          if (event.entry.direction === 'sent') setSent(push);
          else setReceived(push);
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
  const handleSaveConfig = () =>
    guard(async () => {
      const text = JSON.stringify(buildSessionExport(connections), null, 2);
      if (canUseNativeFiles()) {
        await window.pywebview.api.save_config_file(text);
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
      <header className="flex h-11 shrink-0 items-center gap-3 border-b border-slate-800 bg-slate-900 px-3">
        <div className="flex items-center gap-2">
          <Logo size={22} />
          <span className="text-[13px] font-semibold tracking-tight text-slate-100">GSim</span>
          <span className="text-[10px] font-medium uppercase tracking-wider text-slate-600">
            Generic Simulator
          </span>
        </div>

        {/* Save/Load the whole session as one JSON file -- lives here, not in
            the Connections panel header, because that header is too narrow to
            reliably show both buttons without one landing under the Inspector
            layout next to it (the original bug report). */}
        <span className="mx-1 h-4 w-px shrink-0 bg-slate-800" />
        <IconButton
          icon={Download}
          title={connections.length ? 'Save connections to a file' : 'No connections to save'}
          disabled={!connections.length}
          onClick={handleSaveConfig}
        />
        <IconButton icon={Upload} title="Load connections from a file" onClick={handleLoadConfigClick} />

        <span className="mx-1 h-4 w-px shrink-0 bg-slate-800" />
        <IconButton
          icon={theme === 'light' ? Moon : Sun}
          title={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
          onClick={() => setTheme((current) => (current === 'light' ? 'dark' : 'light'))}
        />

        {/* Only surface these when something is actually DOWN -- a permanent
            "connected" chip is noise that says nothing the rest of the UI
            is not already showing.

            The live feed gets its own indicator because losing it is silent
            otherwise: every button still works over HTTP, so the app looks
            healthy while no incoming message, connection-state change or
            clear broadcast ever lands again. */}
        {!online ? (
          <span className="ml-auto flex items-center gap-1.5 text-[10px] font-medium text-rose-400">
            <StatusDot on={false} className="h-1.5 w-1.5 !bg-rose-500" />
            API unreachable
          </span>
        ) : (
          !liveFeed && (
            <span
              className="ml-auto flex items-center gap-1.5 text-[10px] font-medium text-amber-400"
              title="The WebSocket feed is down, so nothing pushed by the server will appear until it reconnects. Actions you take still work."
            >
              <StatusDot on={false} className="h-1.5 w-1.5 !bg-amber-500" />
              Live feed reconnecting…
            </span>
          )
        )}
      </header>

      <div className="flex min-h-0 flex-1">
        {/* Left column: Connections above, the selected connection's Messages
            below -- browsing a connection and its messages is one continuous
            gesture, so they belong in one column. */}
        <div className="flex w-64 shrink-0 flex-col border-r border-slate-800">
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
                await (connection.running ? api.stop(connection.name) : api.start(connection.name));
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
          />
        </main>

        <Console
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
          existing={behaviours.find(
            (behaviour) =>
              behaviour.connection_name === behaviourDraft.connectionName &&
              behaviour.unit_name === behaviourDraft.unitName &&
              behaviour.op_code === behaviourDraft.opCode,
          )}
          onSubmit={({ kind, interval }) =>
            api.setBehaviour(behaviourDraft.connectionName, {
              unit_name: behaviourDraft.unitName,
              op_code: behaviourDraft.opCode,
              kind,
              interval,
              payload: behaviourDraft.payload,
            })
          }
          {...behaviourActions}
          onClose={() => setBehaviourDraft(null)}
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
              notify(`Saved to ${path}`);
            } else {
              const { contents } = await api.readFile(path);
              await importConnections(parseSessionImport(contents));
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
