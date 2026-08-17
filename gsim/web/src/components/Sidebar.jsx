/**
 * Connections sidebar.
 *
 * The status dot is the row's primary affordance *and* its toggle: clicking it
 * starts/stops the connection without changing the selection, so on/off never
 * costs you your place. Row selection is what drives every other panel.
 */
import React from 'react';
import { Cable, Pencil, Plus, Trash2 } from 'lucide-react';
import { Badge, EmptyState, IconButton, Panel, PanelHeader, StatusDot, cx } from './ui';

export default function Sidebar({
  connections, selectedName, onSelect, onCreate, onEdit, onDelete, onToggle, className,
}) {
  const selected = connections.find((connection) => connection.name === selectedName) ?? null;

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
              return (
                <li key={connection.name}>
                  <div
                    role="button"
                    tabIndex={0}

                    onClick={() => onSelect(connection.name)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        onSelect(connection.name);
                      }
                    }}
                    className={cx(
                      'group flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1.5',
                      'transition-colors duration-100',
                      'focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/70',
                      isSelected
                        ? 'bg-slate-800 ring-1 ring-slate-700'
                        : 'hover:bg-slate-800/60',
                    )}
                  >
                    <button
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();   // toggling must not re-select
                        onToggle(connection);
                      }}
                      title={connection.running ? 'Running — click to stop' : 'Stopped — click to start'}
                      aria-label={connection.running ? 'Stop connection' : 'Start connection'}
                      className="grid h-5 w-5 shrink-0 place-items-center rounded transition-colors hover:bg-slate-700/70 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500"
                    >
                      <StatusDot on={connection.running} />
                    </button>

                    <div className="min-w-0 flex-1">
                      <div
                        className={cx(
                          'truncate text-xs font-medium',
                          isSelected ? 'text-slate-100' : 'text-slate-300',
                        )}
                      >
                        {connection.name}
                      </div>
                      <div className="truncate font-mono text-[10px] text-slate-500">
                        {connection.protocol}/{connection.side} · unit {connection.unit_code}
                      </div>
                    </div>

                    {connection.peers?.length > 1 && (
                      <Badge tone="slate">{connection.peers.length}</Badge>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {selected && (
        <footer className="shrink-0 border-t border-slate-800 px-3 py-2">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Connected units
          </div>
          <ul className="flex flex-col gap-0.5">
            {selected.peers.map((peer) => (
              <li key={peer.name} className="flex items-center gap-1.5 text-[11px]">
                <StatusDot on={selected.active_units?.includes(peer.name)} className="h-1.5 w-1.5" />
                <span className="truncate text-slate-400">{peer.name}</span>
                <span className="ml-auto font-mono text-[10px] text-slate-600">
                  {peer.unit_code}
                </span>
              </li>
            ))}
          </ul>
        </footer>
      )}
    </Panel>
  );
}
