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

  // Above this many sends a second, reading the rate off the console means
  // eyeballing the +Nms deltas on rows that are replacing themselves faster
  // than they can be counted. The console is truthful (it carries every entry,
  // unsampled) but it is not a rate meter, so a fast schedule gets a number.
  const UNCOUNTABLE_HZ = 20;

  // What the schedule is ACTUALLY delivering -- shown whether or not it is on
  // target. "Is it really sending at 1kHz?" is the exact question someone
  // setting a 0.001s interval is asking, and answering it only when the answer
  // is "no" leaves the good case indistinguishable from an unanswered one.
  // Amber when genuinely short of the request, green when meeting it.
  const rateOf = (behaviour) => {
    if (!behaviour.active || !behaviour.actual_hz || !behaviour.interval) return null;
    const requested = 1 / behaviour.interval;
    const short = behaviour.actual_hz < requested * 0.95;
    if (requested <= UNCOUNTABLE_HZ && !short) return null;  // 'every 2s' says it better
    const hz = behaviour.actual_hz;
    return { label: `${hz >= 100 ? Math.round(hz) : hz.toFixed(1)} Hz`, short };
  };

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
            // Three SIBLING controls, not two buttons nested inside a
            // clickable div: the old shape announced itself as a button
            // containing buttons and made Enter/Space on the row ambiguous.
            <li
              key={behaviour.id}
              className={cx(
                'group flex flex-col rounded-md px-2 py-1.5',
                'transition-colors duration-100 hover:bg-slate-800/60',
              )}
            >
              <div className="flex w-full items-center gap-2">
                <button
                  type="button"
                  onClick={() => (behaviour.enabled ? onStop : onStart)(behaviour.id)}
                  title={behaviour.enabled ? 'Running — click to stop' : 'Stopped — click to start'}
                  aria-label={
                    behaviour.enabled
                      ? `Stop ${behaviour.message_name ?? behaviour.op_code_hex}`
                      : `Start ${behaviour.message_name ?? behaviour.op_code_hex}`
                  }
                  className="grid h-5 w-5 shrink-0 place-items-center rounded transition-colors hover:bg-slate-700/70 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500"
                >
                  {/* `active` (really firing), not `enabled` (intended to) --
                      a behaviour armed on a stopped connection must not read
                      as live. */}
                  <StatusDot on={behaviour.active} />
                </button>

                <button
                  type="button"
                  onClick={() => onEdit(behaviour)}
                  className="min-w-0 flex-1 rounded text-left focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/70"
                >
                  <span className="block truncate text-xs font-medium text-slate-300">
                    {behaviour.message_name ?? behaviour.op_code_hex}
                  </span>
                  <span className="block truncate font-mono text-[10px] text-slate-500">
                    {nameOf(behaviour.connection_name)} → {behaviour.unit_name} ·{' '}
                    {/* The measured rate REPLACES the interval once there is
                        one, rather than sitting beside it: this line is narrow
                        enough to truncate, and in a rail that can only fit one
                        of the two, what it is doing beats what it was told. */}
                    {rateOf(behaviour)
                      ? (
                        <span className={rateOf(behaviour).short ? 'text-amber-500/90' : 'text-emerald-500/80'}>
                          {rateOf(behaviour).label}
                        </span>
                      )
                      : `every ${behaviour.interval}s`}
                  </span>
                </button>

                <span
                  className="tnum shrink-0 font-mono text-[10px] text-slate-500"
                  title={[
                    `${behaviour.sent_count} sent`,
                    behaviour.actual_hz ? `${behaviour.actual_hz.toFixed(1)} Hz measured` : null,
                    behaviour.missed_ticks ? `${behaviour.missed_ticks} ticks missed` : null,
                  ].filter(Boolean).join('\n')}
                >
                  {behaviour.sent_count}
                </span>
                <IconButton
                  icon={Trash2} title="Remove behaviour" variant="danger"
                  onClick={() => onDelete(behaviour.id)}
                  className="!h-5 !w-5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                />
              </div>

              {/* The error itself, not just a triangle with the text hidden in
                  a tooltip. A failing schedule is the single most useful thing
                  this panel can tell you, and "peer not connected yet" reads
                  very differently from "payload cannot encode" -- which is
                  exactly the distinction a hover-only label withheld. */}
              {behaviour.last_error && (
                <p
                  className="mt-1 flex items-start gap-1 pl-7 text-[10px] leading-snug text-amber-300"
                  title={behaviour.last_error}
                >
                  <TriangleAlert size={10} className="mt-px shrink-0" />
                  <span className="line-clamp-2 min-w-0">{behaviour.last_error}</span>
                </p>
              )}
            </li>
          ))}
        </ul>
      </div>
    </Panel>
  );
}
