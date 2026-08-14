/**
 * Message console: two stacked panes, Sent above Received.
 *
 * Both are PROCESS-WIDE, not scoped to the selected connection. That is not a
 * preference -- it is required for the console to be truthful. A send is logged
 * against the SENDER and the matching receive against the RECIPIENT, which are
 * two different GSim connections, so a per-connection console can only ever
 * show one half of any exchange: send from A to B with A selected and the
 * inbound copy is invisible until you go click B. Showing every connection puts
 * both halves of a round trip on screen at once, which is the point of a
 * simulator console.
 *
 * The 'Hide' rule is per-opCode and forward-looking: hiding an entry hides every
 * entry in THAT PANE sharing its opCode, including ones that have not arrived
 * yet. Holding the hidden set as opCodes (not marking entries) is what makes
 * "and future messages" free -- new entries are filtered on render, so nothing
 * has to be re-marked as it streams in. The two panes keep independent sets, so
 * muting your own outbound chatter on an opcode never blinds you to the replies
 * it provokes (this project's layouts routinely reuse one opcode both ways).
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDownLeft, ArrowUpRight, Eye, EyeOff, TriangleAlert } from 'lucide-react';
import { Badge, EmptyState, IconButton, cx } from './ui';

export default function Console({ sent, received, selected, onSelect }) {
  return (
    <div className="flex w-[27rem] shrink-0 flex-col border-l border-slate-800">
      <LogPane
        title="Sent"
        direction="sent"
        entries={sent}
        selected={selected}
        onSelect={onSelect}
        className="min-h-0 flex-1 border-b border-slate-800"
      />
      <LogPane
        title="Received"
        direction="received"
        entries={received}
        selected={selected}
        onSelect={onSelect}
        className="min-h-0 flex-1"
      />
    </div>
  );
}

function LogPane({ title, direction, entries, selected, onSelect, className }) {
  const [hidden, setHidden] = useState(() => new Set());
  const [follow, setFollow] = useState(true);
  const scrollerRef = useRef(null);

  const isSent = direction === 'sent';
  const Arrow = isSent ? ArrowUpRight : ArrowDownLeft;
  const tone = isSent ? 'sky' : 'emerald';

  const visible = useMemo(
    () => entries.filter((entry) => !hidden.has(entry.op_code)),
    [entries, hidden],
  );
  // Only counts entries suppressed by the hide rule -- never conflated with
  // anything else, so the number always explains itself.
  const hiddenCount = entries.length - visible.length;

  // Tail the log, but only while the user is already at the bottom -- yanking
  // the viewport while they read scrollback is the classic console sin.
  useEffect(() => {
    if (!follow) return;
    const scroller = scrollerRef.current;
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }, [visible.length, follow]);

  const onScroll = (event) => {
    const { scrollTop, scrollHeight, clientHeight } = event.currentTarget;
    setFollow(scrollHeight - scrollTop - clientHeight < 24);
  };

  return (
    <section className={cx('flex flex-col bg-slate-900', className)}>
      <header className="flex h-9 shrink-0 items-center gap-2 border-b border-slate-800 px-3">
        <Arrow size={13} className={isSent ? 'text-sky-400' : 'text-emerald-400'} />
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          {title}
        </h2>
        <span className="rounded bg-slate-800 px-1.5 py-px font-mono text-[10px] text-slate-400">
          {visible.length}
        </span>
        {hiddenCount > 0 && (
          <span className="flex items-center gap-1 text-[10px] text-slate-500">
            <EyeOff size={10} />
            {hiddenCount} hidden
          </span>
        )}
        <div className="ml-auto flex items-center gap-1">
          {!follow && (
            <button
              type="button"
              onClick={() => setFollow(true)}
              className="rounded px-1.5 py-0.5 text-[10px] font-medium text-sky-400 hover:bg-slate-800"
            >
              ↓ Follow
            </button>
          )}
          <IconButton
            icon={Eye}
            title={hidden.size ? `Display all (${hidden.size} opCode filters)` : 'Nothing hidden'}
            disabled={hidden.size === 0}
            onClick={() => setHidden(new Set())}
          />
        </div>
      </header>

      <div ref={scrollerRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
        {visible.length === 0 ? (
          <EmptyState>
            {entries.length === 0
              ? isSent
                ? 'Nothing sent yet.'
                : 'Nothing received yet.'
              : 'Everything in this pane is hidden.'}
          </EmptyState>
        ) : (
          <ul className="flex flex-col py-1">
            {visible.map((entry) => (
              <LogRow
                key={entry.seq}
                entry={entry}
                tone={tone}
                Arrow={Arrow}
                isSelected={selected?.seq === entry.seq}
                onSelect={() => onSelect(entry)}
                onHide={() => setHidden((current) => new Set(current).add(entry.op_code))}
              />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function LogRow({ entry, tone, Arrow, isSelected, onSelect, onHide }) {
  const isSent = tone === 'sky';
  const time = new Date(entry.timestamp * 1000).toLocaleTimeString(undefined, { hour12: false });

  return (
    <li
      onClick={onSelect}
      className={cx(
        'group animate-row-in flex cursor-pointer items-center gap-2 px-2 py-1',
        'border-l-2 transition-colors duration-100',
        isSelected
          ? 'border-l-sky-500 bg-slate-800'
          : cx(
              'border-l-transparent hover:bg-slate-800/50',
              isSent ? 'hover:border-l-sky-500/40' : 'hover:border-l-emerald-500/40',
            ),
      )}
    >
      <span className="shrink-0 font-mono text-[10px] text-slate-600">{time}</span>
      <Arrow size={12} className={cx('shrink-0', isSent ? 'text-sky-400' : 'text-emerald-400')} />

      {/* Which GSim connection owns this entry -- needed now the console spans
          every connection rather than just the selected one. */}
      <span className="shrink-0 truncate font-mono text-[9px] text-slate-600">
        {entry.connection_name}
      </span>

      {/* "[name]: [message]" -- for received, `unit_name` is the SENDER's
          configured name, not ours. */}
      <span className="min-w-0 flex-1 truncate text-[11px]">
        <span className={cx('font-medium', isSent ? 'text-sky-300' : 'text-emerald-300')}>
          {entry.unit_name}
        </span>
        <span className="text-slate-600">: </span>
        <span className="text-slate-300">{entry.message_name}</span>
      </span>

      {entry.error && <TriangleAlert size={11} className="shrink-0 text-rose-400" />}

      <Badge tone={tone} className="shrink-0">
        {entry.op_code_hex}
      </Badge>

      <IconButton
        icon={EyeOff}
        title={`Hide all ${entry.direction} messages on ${entry.op_code_hex}`}
        onClick={(event) => {
          event.stopPropagation();
          onHide();
        }}
        className="!h-5 !w-5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
      />
    </li>
  );
}
