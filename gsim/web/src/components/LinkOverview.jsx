/**
 * What the workspace shows when nothing is selected.
 *
 * That state is not an edge case -- it is how the app starts and what you
 * return to every time you delete a selection, and it used to spend roughly 45%
 * of the window on one centred sentence while Connections, Connected units,
 * Messages and Behaviours fought over a 256px rail beside it.
 *
 * It answers "what am I looking at": this unit, every link it has, and whether
 * anything is moving on them. Deliberately built only from what the app already
 * holds -- the connection record and the console's own entries -- so it needs no
 * new endpoint and cannot disagree with the panels around it.
 *
 * It reports "last seen", never a total. The console keeps a bounded window of
 * recent entries (`LOG_VIEW_LIMIT` in App.jsx), so a count taken from it would
 * be a count of the last N messages wearing the label of a lifetime total --
 * the kind of number that is worse than no number.
 */
import React from 'react';
import { Cable, Radio, Share2 } from 'lucide-react';
import { Badge, EmptyState, Panel, PanelHeader, StatusDot, cx, sideLabel } from './ui';
import { formatTime, hex, hexTitle } from '../lib/format';

/** The most recent entry involving one peer, in one direction. Entries are
 *  ordered by `seq`, so the last match is the newest. */
function lastFor(entries, connectionName, peerName) {
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const entry = entries[i];
    if (entry.connection_name === connectionName && entry.unit_name === peerName) return entry;
  }
  return null;
}

/** The structures module(s) this link declared, as core spells them. Falls back
 *  through the connection-level key for configs saved before structures moved
 *  onto each peer. */
function structuresFor(config, peerName) {
  const peer = config?.connections?.[peerName] ?? {};
  const declared = peer.Structures ?? peer.structures ?? config?.Structures;
  if (!declared) return null;
  return Array.isArray(declared) ? declared : [declared];
}

/** `Structures.Tiful.tiful_to_dtu` -> `Tiful.tiful_to_dtu`: the tail is what
 *  identifies the module, and the head never varies. */
function shortModule(name) {
  const parts = String(name).split(/[.\\/]/).filter(Boolean);
  return parts.slice(-2).join('.');
}

export default function LinkOverview({ connection, sent, received, onPickPeer }) {
  if (!connection) {
    return (
      <Panel className="min-w-0 flex-1">
        <PanelHeader title="Links" icon={Share2} rank="workspace" />
        <EmptyState icon={Cable}>
          Select a connection to see its links, or create one with + in the
          Connections panel.
        </EmptyState>
      </Panel>
    );
  }

  const { config = {}, peers = [], active_units: activeUnits = [] } = connection;
  const isMulticast = connection.protocol === 'multicast';
  // `connecting` -- still INSIDE `unit.start()`, retrying a refused connect
  // once a second (`Connection._startup_all`, core/connections/base.py) --
  // reads as "on" here same as `running`: without it this dot (and the badge)
  // sat on "stopped" for however long the retry took, then jumped straight to
  // running with nothing on screen ever having said it was trying.
  const on = connection.running || connection.connecting;
  const waitingForPeer = connection.running && activeUnits.length === 0;

  return (
    <Panel className="min-w-0 flex-1">
      <PanelHeader title="Links" icon={Share2} rank="workspace">
        <Badge tone={on ? 'emerald' : 'slate'}>
          {connection.connecting ? 'connecting' : connection.running ? 'running' : 'stopped'}
        </Badge>
      </PanelHeader>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <div className="mx-auto flex max-w-3xl flex-col gap-2.5">
          {/* Who we are on the wire. */}
          <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-3">
            <div className="flex items-center gap-2">
              {/* Amber while dialling, or while running with no peer actually
                  connected yet (a server nobody has dialled into). Gray only
                  when genuinely off. */}
              <StatusDot on={on} pending={connection.connecting || waitingForPeer} />
              <h3 className="truncate text-sm font-semibold text-slate-100">{connection.name}</h3>
            </div>
            <dl className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-slate-800 pt-2.5">
              <Fact label="protocol" value={connection.protocol} />
              <Fact label="side" value={sideLabel(connection.protocol, connection.side)} />
              <Fact
                label="our unitCode"
                value={hex(connection.unit_code, 2)}
                title={hexTitle(connection.unit_code, 'unitCode')}
              />
              {config.local_ip && <Fact label="local ip" value={config.local_ip} />}
              {isMulticast && config.ip && <Fact label="group" value={config.ip} />}
            </dl>
            {connection.start_error && (
              <p className="mt-2.5 border-t border-slate-800 pt-2.5 font-mono text-[11px] text-amber-300">
                Not started: {connection.start_error}
              </p>
            )}
          </div>

          {/* One card per link. Clicking one aims the Messages panel at it, so
              this view is a way IN to composing rather than a dead end. */}
          <div className="flex flex-col gap-2">
            {peers.map((peer) => {
              const live = activeUnits.includes(peer.name);
              const spec = config?.connections?.[peer.name] ?? {};
              const modules = structuresFor(config, peer.name);
              const lastSent = lastFor(sent, connection.name, peer.name);
              const lastReceived = lastFor(received, connection.name, peer.name);

              return (
                <button
                  key={peer.name}
                  type="button"
                  onClick={() => onPickPeer?.(peer.name)}
                  className={cx(
                    'group flex flex-col gap-2 rounded-lg border p-3 text-left transition-colors',
                    'focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/70',
                    live
                      ? 'border-emerald-900/60 bg-emerald-950/10 hover:border-emerald-800'
                      : 'border-slate-800 bg-slate-900/40 hover:border-slate-700',
                  )}
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <StatusDot on={on} pending={connection.connecting || (connection.running && !live)} />
                    <span className="truncate text-[13px] font-medium text-slate-100">
                      {peer.name}
                    </span>
                    <Badge tone="slate" className="shrink-0">
                      <span title={hexTitle(peer.unit_code, `${peer.name} unitCode`)}>
                        {hex(peer.unit_code, 2)}
                      </span>
                    </Badge>
                    {spec.port !== undefined && (
                      <span className="tnum shrink-0 font-mono text-[10px] text-slate-500">
                        :{spec.port}
                      </span>
                    )}
                    <span className="ml-auto shrink-0 text-[10px] font-medium text-slate-500 opacity-0 transition-opacity group-hover:opacity-100">
                      compose →
                    </span>
                  </div>

                  {modules && (
                    <div className="flex min-w-0 items-baseline gap-1.5">
                      <span className="shrink-0 text-[10px] font-medium uppercase tracking-wider text-slate-500">
                        IRS
                      </span>
                      <span
                        className="truncate font-mono text-[10px] text-slate-400"
                        title={modules.join(', ')}
                      >
                        {modules.map(shortModule).join(', ')}
                      </span>
                    </div>
                  )}

                  <div className="grid grid-cols-2 gap-2 border-t border-slate-800/70 pt-2">
                    <LastExchange label="last sent" tone="sky" entry={lastSent} />
                    <LastExchange label="last received" tone="emerald" entry={lastReceived} />
                  </div>
                </button>
              );
            })}
          </div>

          <p className="flex items-center gap-1.5 px-1 font-mono text-[10px] text-slate-500">
            <Radio size={11} />
            Last seen is taken from the console's current window, not a lifetime total.
          </p>
        </div>
      </div>
    </Panel>
  );
}

function Fact({ label, value, title }) {
  return (
    <div className="flex items-baseline gap-1.5" title={title}>
      <dt className="text-[10px] font-medium uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="font-mono text-[11px] text-slate-200">{value}</dd>
    </div>
  );
}

function LastExchange({ label, tone, entry }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="text-[10px] font-medium uppercase tracking-wider text-slate-500">
        {label}
      </span>
      {entry ? (
        <>
          <span
            className={cx(
              'truncate text-[11px] font-medium',
              tone === 'sky' ? 'text-sky-300' : 'text-emerald-300',
            )}
          >
            {entry.message_name}
          </span>
          <span className="tnum font-mono text-[10px] text-slate-500">
            {formatTime(entry.timestamp)} · {entry.op_code_hex}
          </span>
        </>
      ) : (
        <span className="text-[11px] text-slate-500">—</span>
      )}
    </div>
  );
}
