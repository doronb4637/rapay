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
import React, { useCallback, useEffect, useState } from 'react';
import { api, openEventStream } from './api';
import Logo from './components/Logo';
import Sidebar from './components/Sidebar';
import MessagesTable from './components/MessagesTable';
import Inspector from './components/Inspector';
import Console from './components/Console';
import ConnectionModal from './components/ConnectionModal';
import { StatusDot, cx } from './components/ui';

export default function App() {
  const [connections, setConnections] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [sent, setSent] = useState([]);
  const [received, setReceived] = useState([]);
  const [selectedLog, setSelectedLog] = useState(null);
  const [selection, setSelection] = useState(null);   // Inspector mode
  const [modal, setModal] = useState(null);           // null | {} | connection
  const [online, setOnline] = useState(false);
  const [toast, setToast] = useState(null);
  // Which peer we are composing TO. Chosen before the message list, because
  // each link has its own IRS: the set of messages this connection can send
  // genuinely differs per destination, so a single union list would offer rows
  // that fail on send.
  const [destination, setDestination] = useState(null);

  const selected = connections.find((c) => c.id === selectedId) ?? null;
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

  // Backfill the console ONCE, across every connection -- not per selection.
  // A send is logged against the sender and its matching receive against the
  // recipient, so scoping the console to the selected connection would hide
  // one half of every exchange (that was the "no received messages" bug).
  useEffect(() => {
    let cancelled = false;
    Promise.all([api.allLogs('sent'), api.allLogs('received')]).then(
      ([s, r]) => {
        if (cancelled) return;
        setSent(s);
        setReceived(r);
      },
      () => {},
    );
    return () => { cancelled = true; };
  }, []);

  // Changing connection only re-aims the Inspector; the console keeps streaming.
  useEffect(() => {
    setSelectedLog(null);
    setSelection(null);
  }, [selectedId]);

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
        if (event.type === 'snapshot') return setConnections(event.connections);
        if (event.type === 'connection.state') return refresh();
        if (event.type === 'connection.deleted') {
          // Drop the departed connection's history so the console does not
          // keep referring to something the user just removed.
          const gone = event.connection_id;
          const drop = (list) => list.filter((entry) => entry.connection_id !== gone);
          setSent(drop);
          setReceived(drop);
          return refresh();
        }
        if (event.type === 'message.sent' || event.type === 'message.received') {
          const push = (list) => [...list, event.entry].slice(-2000);
          if (event.entry.direction === 'sent') setSent(push);
          else setReceived(push);
        }
      }),
    [refresh],
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

  const pickLog = (entry) => {
    setSelectedLog(entry);
    setSelection({ mode: 'inspect', entry });
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

        {/* Only surface the API link when it is actually DOWN -- a permanent
            "connected" chip is noise that says nothing the rest of the UI
            is not already showing. */}
        {!online && (
          <span className="ml-auto flex items-center gap-1.5 text-[10px] font-medium text-rose-400">
            <StatusDot on={false} className="h-1.5 w-1.5 !bg-rose-500" />
            API unreachable
          </span>
        )}
      </header>

      <div className="flex min-h-0 flex-1">
        {/* Left column: Connections above, the selected connection's Messages
            below -- browsing a connection and its messages is one continuous
            gesture, so they belong in one column. */}
        <div className="flex w-64 shrink-0 flex-col border-r border-slate-800">
          {/* Messages gets ~30% more height than Connections (1 : 1.3). */}
          <Sidebar
            className="min-h-0 flex-[1] border-b border-slate-800"
            connections={connections}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onCreate={() => setModal({})}
            onEdit={(connection) => setModal(connection)}
            onDelete={(connection) =>
              guard(async () => {
                await api.deleteConnection(connection.id);
                if (connection.id === selectedId) setSelectedId(null);
                await refresh();
              })
            }
            onToggle={(connection) =>
              guard(async () => {
                await (connection.running ? api.stop(connection.id) : api.start(connection.id));
                await refresh();
              })
            }
          />
          <MessagesTable
            className="min-h-0 flex-[1.3]"
            connectionId={selectedId}
            peers={peers}
            destination={destination}
            onDestinationChange={setDestination}
            activeOpCode={selection?.mode === 'compose' ? selection.opCode : null}
            onCompose={(opCode) => {
              setSelectedLog(null);
              setSelection({ mode: 'compose', opCode });
            }}
          />
        </div>

        <main className="flex min-w-0 flex-1 flex-col">
          <Inspector
            connectionId={selectedId}
            selection={selection}
            peers={peers}
            destination={destination}
            onSent={() => {}}
          />
        </main>

        <Console sent={sent} received={received} selected={selectedLog} onSelect={pickLog} />
      </div>

      {modal && (
        <ConnectionModal
          initial={modal.id ? toForm(modal) : null}
          onSubmit={async (body) => {
            if (modal.id) await api.updateConnection(modal.id, body);
            else await api.createConnection(body);
            await refresh();
          }}
          onClose={() => setModal(null)}
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
  return {
    name: connection.name,
    protocol: config.protocol,
    side: config.side,
    ip: config.ip,
    local_ip: config.local_ip,
    unitCode: config.unitCode,
    peers: Object.entries(config.connections ?? {}).map(([name, spec]) => ({
      name, port: spec.port, unitCode: spec.unitCode,
      // Each link carries its own layouts; core writes the canonical spelling.
      structures: spec.Structures ?? spec.structures ?? [''],
    })),
    structures: config.Structures ?? [''],
    echo_opcode: config.echo_opcode ?? '',
    echo_interval: config.EchoInterval ?? '',
    echo_timeout: config.EchoTimeout ?? '',
  };
}
