/**
 * Behaviours panel: everything currently scheduled, across every connection.
 *
 * Process-wide on purpose, and that is the whole reason this panel exists
 * separately from the badge on a Messages row. A schedule keeps firing while
 * you look at a different connection, so a view scoped to the selection could
 * show an idle screen while traffic streams out of a connection one click away.
 * This is the one place that always answers "what is sending right now?".
 *
 * Rendered only when at least one behaviour exists, so it costs the layout
 * nothing until it has something to say.
 */
import React, { useState } from 'react';
import { ChevronDown, ChevronUp, Play, Repeat, Square, Trash2, TriangleAlert } from 'lucide-react';
import { IconButton, Panel, PanelHeader, StatusDot, cx } from './ui';
import ContextMenu, { useContextMenu } from './ContextMenu';

export default function BehavioursPanel({
  behaviours, connections, onEdit, onStart, onStop, onDelete, className,
}) {
  const menu = useContextMenu();
  // Collapsed hides the LIST but keeps the header -- the count and the running
  // tally stay visible, so minimising never hides the fact that something is
  // still sending. That is the whole reason this panel exists.
  const [collapsed, setCollapsed] = useState(false);
  const nameOf = (connectionName) =>
    connections.find((connection) => connection.name === connectionName)?.name ?? connectionName;

  const running = behaviours.filter((behaviour) => behaviour.active).length;

  const menuItems = [
    {
      label: 'Stop all',
      icon: Square,
      disabled: running === 0,
      hint: running ? String(running) : undefined,
      onSelect: () => behaviours.filter((b) => b.enabled).forEach((b) => onStop(b.id)),
    },
    {
      label: 'Remove all',
      icon: Trash2,
      danger: true,
      disabled: behaviours.length === 0,
      onSelect: () => behaviours.forEach((behaviour) => onDelete(behaviour.id)),
    },
  ];

  return (
    <Panel
      // `shrink-0` while collapsed so the header alone is all the column gives
      // it; the caller's max-height only applies once the list is showing.
      className={cx(className, collapsed && '!max-h-none shrink-0')}
      onContextMenu={menu.open}
    >
      {menu.menuProps && <ContextMenu {...menu.menuProps} items={menuItems} />}
      <PanelHeader title="Behaviours" icon={Repeat} count={behaviours.length}>
        {/* Running tally stays in the header, so a collapsed panel still
            reports that traffic is going out. */}
        {running > 0 && (
          <span className="flex items-center gap-1 text-[10px] font-medium text-emerald-400">
            <StatusDot on className="h-1.5 w-1.5" />
            {running}
          </span>
        )}
        <IconButton
          icon={collapsed ? ChevronUp : ChevronDown}
          title={collapsed ? 'Maximize behaviours' : 'Minimize behaviours'}
          onClick={() => setCollapsed((current) => !current)}
        />
      </PanelHeader>

      <div className={cx('min-h-0 flex-1 overflow-y-auto p-1.5', collapsed && 'hidden')}>
        <ul className="flex flex-col gap-0.5">
          {behaviours.map((behaviour) => (
            <li key={behaviour.id}>
              <div
                role="button"
                tabIndex={0}
                onClick={() => onEdit(behaviour)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    onEdit(behaviour);
                  }
                }}
                title={behaviour.last_error ?? 'Click to edit'}
                className={cx(
                  'group flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1.5',
                  'transition-colors duration-100 hover:bg-slate-800/60',
                  'focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/70',
                )}
              >
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();   // toggling must not open the editor
                    (behaviour.enabled ? onStop : onStart)(behaviour.id);
                  }}
                  title={behaviour.enabled ? 'Running — click to stop' : 'Stopped — click to start'}
                  aria-label={behaviour.enabled ? 'Stop behaviour' : 'Start behaviour'}
                  className="grid h-5 w-5 shrink-0 place-items-center rounded transition-colors hover:bg-slate-700/70 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500"
                >
                  {/* `active` (really firing), not `enabled` (intended to) --
                      a behaviour armed on a stopped connection must not read
                      as live. */}
                  <StatusDot on={behaviour.active} />
                </button>

                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium text-slate-300">
                    {behaviour.message_name ?? behaviour.op_code_hex}
                  </div>
                  <div className="truncate font-mono text-[10px] text-slate-500">
                    {nameOf(behaviour.connection_name)} → {behaviour.unit_name} · every {behaviour.interval}s
                  </div>
                </div>

                {behaviour.last_error && (
                  <TriangleAlert
                    size={11}
                    className="shrink-0 text-amber-400"
                    title={behaviour.last_error}
                  />
                )}
                <span className="shrink-0 font-mono text-[10px] text-slate-600">
                  {behaviour.sent_count}
                </span>
                <IconButton
                  icon={Trash2} title="Remove behaviour" variant="danger"
                  onClick={(event) => {
                    event.stopPropagation();
                    onDelete(behaviour.id);
                  }}
                  className="!h-5 !w-5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                />
              </div>
            </li>
          ))}
        </ul>
      </div>
    </Panel>
  );
}
