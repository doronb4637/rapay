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
import { ArrowDownLeft, ArrowUpRight, Eye, EyeOff, Pause, Trash2, TriangleAlert } from 'lucide-react';
import { Badge, EmptyState, IconButton, cx } from './ui';
import ContextMenu, { useContextMenu } from './ContextMenu';

export default function Console({ sent, received, selected, onSelect, onClear }) {
  return (
    <div className="flex w-[27rem] shrink-0 flex-col border-l border-slate-800">
      <LogPane
        title="Sent"
        direction="sent"
        entries={sent}
        selected={selected}
        onSelect={onSelect}
        onClear={onClear}
        className="min-h-0 flex-1 border-b border-slate-800"
      />
      <LogPane
        title="Received"
        direction="received"
        entries={received}
        selected={selected}
        onSelect={onSelect}
        onClear={onClear}
        className="min-h-0 flex-1"
      />
    </div>
  );
}

function LogPane({ title, direction, entries, selected, onSelect, onClear, className }) {
  const [hidden, setHidden] = useState(() => new Set());
  const [follow, setFollow] = useState(true);
  const [pointerInside, setPointerInside] = useState(false);
  // The array rendered while the pane is held still, or null when it is live.
  const [frozen, setFrozen] = useState(null);
  const scrollerRef = useRef(null);
  const menu = useContextMenu();

  const isSent = direction === 'sent';
  const Arrow = isSent ? ArrowUpRight : ArrowDownLeft;
  const tone = isSent ? 'sky' : 'emerald';

  /* A behaviour on a 0.01s interval delivers ~100 entries/second into a pane
     that keeps 30 rows: the whole list is replaced about three times a second.
     Coalescing the state updates (App.jsx) stops the wasted re-renders but not
     this -- every row still slides upward and is evicted within ~300ms, so the
     <li> the user pressed on is a different element, or gone entirely, by the
     time they release. A `click` only fires when mousedown and mouseup land on
     the SAME element, so at that rate the click is not mis-aimed, it is never
     generated at all. No amount of aiming can beat it.

     So the pane stops moving whenever the user is plausibly reaching for a row:
     pointer inside it, or scrolled away from the bottom. `frozen` holds the
     exact array that was on screen at that moment -- a snapshot, not a `seq`
     cutoff, because the rows worth clicking are precisely the ones eviction
     would otherwise drop out from under the cursor. Nothing is lost: the live
     list keeps filling behind the snapshot and the pane rejoins it on thaw. */
  const held = pointerInside || !follow;

  useEffect(() => {
    if (!held) return setFrozen(null);
    // `current ?? ...` is what makes this a snapshot rather than a follow: once
    // taken it survives every later commit, until the hold ends. An empty pane
    // is nothing to aim at, so it stays live until it has a row -- otherwise
    // hovering an idle console pins it at "Nothing sent yet." while traffic
    // starts behind it, which looks exactly like the bug this fixes.
    setFrozen((current) => current ?? (entries.length ? entries : null));
  }, [held, entries]);

  // `frozen` is only ever set to a non-empty list, so indexing its tail is safe.
  const frozenCeiling = frozen ? frozen[frozen.length - 1].seq : null;
  const liveCeiling = entries.length ? entries[entries.length - 1].seq : -1;

  // A Clear (or a connection removal) TRUNCATES the live list instead of
  // growing it, and `seq` never restarts -- so the live tail falling behind the
  // snapshot's is the one unambiguous signal that entries were dropped rather
  // than evicted by the view limit. Without this the snapshot outlives the
  // clear and the pane keeps showing rows the server has already forgotten:
  // "Clear did nothing", the exact failure the server-side clear exists to
  // avoid. Clear is reached from the pane's own context menu, so the pointer is
  // inside -- the pane is always held at that moment.
  useEffect(() => {
    if (frozen && liveCeiling < frozenCeiling) setFrozen(null);
  }, [liveCeiling, frozenCeiling, frozen]);

  const source = frozen ?? entries;
  const visible = useMemo(
    () => source.filter((entry) => !hidden.has(entry.op_code)),
    [source, hidden],
  );
  // Only counts entries suppressed by the hide rule -- never conflated with
  // anything else (including what the freeze is holding back), so the number
  // always explains itself.
  const hiddenCount = source.length - visible.length;

  // Tail the log, but only while the user is already at the bottom -- yanking
  // the viewport while they read scrollback is the classic console sin.
  //
  // Keyed on the NEWEST entry's seq, not on `visible.length`. Once the pane is
  // at its cap every new message evicts an old one, so the length stops
  // changing and a length-keyed effect never fires again -- the pane silently
  // stopped following at exactly the point following matters. `seq` is a
  // server-side counter that only ever increases, so it changes on every
  // arrival whether or not the list grew.
  //
  // Skipped entirely while held: scrolling a frozen list back to the bottom on
  // every arrival would move the rows the freeze exists to hold still.
  const newestSeq = visible.length ? visible[visible.length - 1].seq : null;
  useEffect(() => {
    if (!follow || frozen) return;
    const scroller = scrollerRef.current;
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }, [newestSeq, follow, frozen]);

  const onScroll = (event) => {
    const { scrollTop, scrollHeight, clientHeight } = event.currentTarget;
    // Generous threshold: at the cap, one row leaves the top as another joins
    // the bottom, and a tight bound would flip `follow` off on that jitter and
    // strand the user mid-list.
    setFollow(scrollHeight - scrollTop - clientHeight < 48);
  };

  // Right-click anywhere in the pane -- header, a row, or the empty area.
  // Kept on the whole <section> rather than the list so it still works when
  // the pane is empty, which is exactly when "Clear" is least useful but
  // "why is nothing here" is most likely to be asked.
  const menuItems = [
    {
      label: `Clear ${title}`,
      icon: Trash2,
      danger: true,
      disabled: entries.length === 0,
      hint: entries.length ? String(entries.length) : undefined,
      onSelect: () => onClear?.(direction),
    },
  ];

  return (
    <section
      onContextMenu={menu.open}
      className={cx('flex flex-col bg-slate-900', className)}
    >
      {menu.menuProps && <ContextMenu {...menu.menuProps} items={menuItems} />}
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
        {/* Without this the freeze reads as the feed having died -- which is
            the same symptom the user came here to report. Deliberately carries
            no "N new" count: `entries` is itself capped at the view limit, so
            any such number saturates there and would understate a fast feed
            rather than describe it. */}
        {held && (
          <span
            className="flex items-center gap-1 text-[10px] font-medium text-amber-400"
            title="Held still so rows can be clicked. Resumes when the pointer leaves and the pane is scrolled to the bottom."
          >
            <Pause size={10} />
            paused
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

      {/* `overflow-anchor: none` is load-bearing at the cap. Chrome's scroll
          anchoring compensates when content is removed ABOVE the viewport, so
          each evicted top row pulled scrollTop up by a row while a new row was
          appended below -- the view drifted off the bottom a row at a time even
          though nothing moved it. */}
      {/* Hover is tracked on the SCROLLER, not the section, so the header's own
          Follow / Display-all buttons are reachable without the pane counting
          as held -- clicking Follow has to be able to end the hold. */}
      <div
        ref={scrollerRef}
        onScroll={onScroll}
        onMouseEnter={() => setPointerInside(true)}
        onMouseLeave={() => setPointerInside(false)}
        style={{ overflowAnchor: 'none' }}
        className="min-h-0 flex-1 overflow-y-auto"
      >
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
