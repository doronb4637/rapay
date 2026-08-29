/**
 * Connections sidebar.
 *
 * The status dot is the row's primary affordance *and* its toggle: clicking it
 * starts/stops the connection without changing the selection, so on/off never
 * costs you your place. Row selection is what drives every other panel.
 *
 * The two are SIBLING buttons inside the row, not a button inside a clickable
 * div. The old shape announced itself to assistive technology as a button
 * containing a button, and its own Enter/Space handler competed with the inner
 * one for the same keys; a keyboard user pressing space on the row could not be
 * sure whether they were selecting or toggling. Now the row is a plain <li>, the
 * name is a real button, the dot is a real button, and Tab visits them in the
 * order they are read.
 */
import React from 'react';
import { Cable, ChevronDown, ChevronRight, Pencil, Plus, Trash2 } from 'lucide-react';
import { Badge, EmptyState, IconButton, Panel, PanelHeader, StatusDot, cx, sideLabel } from './ui';
import { readPref, writePref } from '../lib/prefs';
import { hex, hexTitle } from '../lib/format';

export default function Sidebar({
  connections, selectedName, onSelect, onCreate, onEdit, onDelete, onToggle, className,
}) {
  const selected = connections.find((connection) => connection.name === selectedName) ?? null;
  // The footer is a fixed block competing with the list for the column's
  // height, and at a short window it won: with four connections and three peers
  // the fourth row fell out of view with nothing to say it was there. Collapsing
  // it hands the height back, and the choice is remembered.
  const [unitsOpen, setUnitsOpen] = React.useState(() => readPref('sidebar.units', true));
  const toggleUnits = () => {
    setUnitsOpen((open) => {
      writePref('sidebar.units', !open);
      return !open;
    });
  };

  return (
    <Panel className={className}>
      <PanelHeader title="Connections" icon={Cable} count={connections.length}>
        <IconButton icon={Plus} title="Create connection" onClick={onCreate} />
        <IconButton
          icon={Pencil}
          title={selected ? `Edit ${selected.name}` : 'Select a connection to edit'}
          disabled={!selected}
          onClick={() => onEdit(selected)}
        />
        <IconButton
          icon={Trash2}
          title={selected ? `Delete ${selected.name}` : 'Select a connection to delete'}
          variant="danger"
          disabled={!selected}
          onClick={() => onDelete(selected)}
        />
      </PanelHeader>

      <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
        {connections.length === 0 ? (
          <EmptyState icon={Cable}>
            No connections yet. Use <span className="text-slate-400">+</span> to create one.
          </EmptyState>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {connections.map((connection) => {
              const isSelected = connection.name === selectedName;
              // Two distinct "not there yet" states, both amber, told apart
              // only in the tooltip:
              //
              // `dialling` -- core is still INSIDE `unit.start()`. A TCP
              // client whose server refuses the connection now retries once a
              // second forever (`Connection._startup_all`,
              // core/connections/base.py), and GSim no longer blocks the
              // click waiting for that to resolve (`runtime.start()`'s
              // CONNECT_GRACE_SECONDS) -- so `running` is still false while
              // this drags on, and `connecting` is what says so.
              //
              // `waitingForPeer` -- already started (a server bound and
              // listening, or a client that connected), just nothing has
              // shown up yet. `peers.length` guards the one connection shape
              // with no peer to wait for at all (bypassing the create modal,
              // which always requires at least one).
              //
              // Before this, both looked identical to OFF: the dot stayed
              // gray for however long a retry took, then jumped straight to
              // green with nothing on screen ever having said it was trying.
              const dialling = connection.connecting;
              const waitingForPeer = connection.running
                && connection.peers?.length > 0
                && (connection.active_units?.length ?? 0) === 0;
              const on = connection.running || dialling;
              const pending = dialling || waitingForPeer;
              return (
                <li
                  key={connection.name}
                  className={cx(
                    'flex items-center gap-1 rounded-md pl-1 pr-2 transition-colors duration-100',
                    isSelected ? 'bg-slate-800 ring-1 ring-slate-700' : 'hover:bg-slate-800/60',
                  )}
                >
                  <button
                    type="button"
                    onClick={() => onToggle(connection)}
                    title={
                      dialling
                        ? 'Connecting — click to stop'
                        : waitingForPeer
                          ? 'Running, waiting for a peer — click to stop'
                          : connection.running
                            ? 'Running — click to stop'
                            : 'Stopped — click to start'
                    }
                    aria-label={on ? `Stop ${connection.name}` : `Start ${connection.name}`}
                    className="grid h-6 w-6 shrink-0 place-items-center rounded transition-colors hover:bg-slate-700/70 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500"
                  >
                    <StatusDot on={on} pending={pending} />
                  </button>

                  <button
                    type="button"
                    onClick={() => onSelect(connection.name)}
                    aria-pressed={isSelected}
                    className="min-w-0 flex-1 rounded py-1.5 text-left focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/70"
                  >
                    <span
                      className={cx(
                        'block truncate text-xs font-medium',
                        isSelected ? 'text-slate-100' : 'text-slate-300',
                      )}
                    >
                      {connection.name}
                    </span>
                    <span
                      className="block truncate font-mono text-[10px] text-slate-500"
                      title={hexTitle(connection.unit_code, 'unitCode')}
                    >
                      {connection.protocol}/{sideLabel(connection.protocol, connection.side)} ·{' '}
                      {hex(connection.unit_code, 2)}
                    </span>
                  </button>

                  {connection.peers?.length > 1 && (
                    <Badge tone="slate">{connection.peers.length}</Badge>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {selected && (
        <footer className="shrink-0 border-t border-slate-800">
          <button
            type="button"
            onClick={toggleUnits}
            aria-expanded={unitsOpen}
            className="flex w-full items-center gap-1 px-2 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 transition-colors hover:text-slate-300 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/70"
          >
            {unitsOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            Connected units
            <span className="ml-auto font-mono">{selected.peers.length}</span>
          </button>
          {unitsOpen && (
            <ul className="flex flex-col gap-0.5 px-3 pb-2">
              {selected.peers.map((peer) => {
                const active = selected.active_units?.includes(peer.name);
                return (
                <li key={peer.name} className="flex items-center gap-1.5 text-[11px]">
                  {/* `active` implies `selected.running` (stopping a
                      connection clears active_units), so `on` only needs
                      `running`/`connecting` -- same amber-while-dialling
                      treatment as the row's own dot above. */}
                  <StatusDot
                    on={selected.running || selected.connecting}
                    pending={selected.connecting || (selected.running && !active)}
                    className="h-1.5 w-1.5"
                  />
                  <span className="truncate text-slate-400">{peer.name}</span>
                  <span
                    className="ml-auto font-mono text-[10px] text-slate-500"
                    title={hexTitle(peer.unit_code, `${peer.name} unitCode`)}
                  >
                    {hex(peer.unit_code, 2)}
                  </span>
                </li>
                );
              })}
            </ul>
          )}
        </footer>
      )}
    </Panel>
  );
}
