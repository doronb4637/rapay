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
 * Putting both halves on screen is not the same as connecting them, though, so
 * hovering a row also DIMS everything in the other pane that could not be its
 * counterpart -- see `counterpartOf`. That is the cheap half of pairing: it
 * needs no server support and no correlation id, and it answers the question
 * the two panes exist to answer.
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
import { ArrowDownLeft, ArrowUpRight, Eye, EyeOff, ListFilter, Pause, Trash2, TriangleAlert } from 'lucide-react';
import { Badge, EmptyState, IconButton, cx } from './ui';
import ContextMenu, { useContextMenu } from './ContextMenu';
import { formatDelta, formatTime } from '../lib/format';

export default function Console({
  sent, received, selected, onSelect, onClear, filters = [], onOpenFilters, style,
}) {
  // Which entry the pointer is on, wherever it is. Shared across both panes so
  // one can dim against the other; held here because neither pane owns it.
  const [hovered, setHovered] = useState(null);

  return (
    <div className="flex shrink-0 flex-col" style={style}>
      <LogPane
        title="Sent"
        direction="sent"
        entries={sent}
        selected={selected}
        onSelect={onSelect}
        onClear={onClear}
        hovered={hovered}
        onHover={setHovered}
        className="min-h-0 flex-1 border-b border-slate-800"
      />
      <LogPane
        title="Received"
        direction="received"
        entries={received}
        selected={selected}
        onSelect={onSelect}
        onClear={onClear}
        hovered={hovered}
        onHover={setHovered}
        // Received only. A filter drops messages before they are ever logged
        // (server-side, see core_gateway/filters.py) and nothing filters the
        // sent direction, so the Sent pane has nothing to report here.
        filters={filters}
        onOpenFilters={onOpenFilters}
        className="min-h-0 flex-1"
      />
    </div>
  );
}

/**
 * Could `entry` be the other half of `other`'s exchange?
 *
 * Matched on the peer and the opcode, which is as far as this data honestly
 * goes: core's callbacks carry no correlation id, so a stricter claim would be
 * invented. Deliberately loose in the other direction too -- a request and its
 * acknowledgement routinely share an opcode in this project's layouts, which is
 * exactly the case worth lighting up.
 *
 * Used only to DIM the rows that cannot be related, never to hide them: this is
 * a hint about where to look, not a filter.
 */
function counterpartOf(entry, other) {
  if (!other || other.direction === entry.direction) return true;
  return other.unit_name === entry.unit_name && other.op_code === entry.op_code;
}

function LogPane({
  title, direction, entries, selected, onSelect, onClear, className, hovered, onHover,
  filters, onOpenFilters,
}) {
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

  /* The OTHER suppression, and deliberately a separate number from the one
     above. `hidden` is this pane's own instant per-opCode mute: local,
     reversible, and the entries still exist. `dropped` is the server-side
     filters, which reject a message before it is ever logged -- so those
     entries are genuinely gone. Conflating them into one "not shown" count
     would hide exactly the distinction that matters when deciding whether the
     thing you are looking for can still be recovered. */
  const armedFilters = (filters ?? []).filter((entry) => entry.armed);
  const droppedCount = armedFilters.reduce((sum, entry) => sum + entry.dropped, 0);

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

  // Whether the OTHER pane is being pointed at. Only then is dimming meaningful
  // -- inside one's own pane every row is trivially "related".
  const pairing = hovered && hovered.direction !== direction ? hovered : null;

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
        {droppedCount > 0 && (
          <button
            type="button"
            onClick={onOpenFilters}
            title={`${droppedCount.toLocaleString()} messages dropped by ${armedFilters.length} filter${armedFilters.length === 1 ? '' : 's'} before they were logged. Click to review.`}
            className="tnum flex items-center gap-1 rounded px-1 text-[10px] text-amber-500/90 hover:bg-slate-800"
          >
            <ListFilter size={10} />
            {droppedCount.toLocaleString()} dropped
          </button>
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
          {onOpenFilters && (
            <IconButton
              icon={ListFilter}
              title={armedFilters.length
                ? `${armedFilters.length} filter${armedFilters.length === 1 ? '' : 's'} deciding what gets logged`
                : 'Filter what gets logged'}
              onClick={onOpenFilters}
              className={armedFilters.length ? '!text-amber-400' : undefined}
            />
          )}
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
        onMouseLeave={() => { setPointerInside(false); onHover?.(null); }}
        style={{ overflowAnchor: 'none' }}
        className="min-h-0 flex-1 overflow-y-auto"
      >
        {visible.length === 0 ? (
          <EmptyState>
            {entries.length === 0 && armedFilters?.length > 0 ? (
              /* An empty pane with filters armed is ambiguous in the worst
                 possible way: it looks exactly like a dead link. Say which one
                 it is, and put the way out one click away -- the pane must
                 never be the place where the app goes quiet without explaining
                 itself. */
              <>
                Nothing has passed your filters yet.{' '}
                <button
                  type="button"
                  onClick={onOpenFilters}
                  className="rounded text-sky-400 underline decoration-dotted underline-offset-2 hover:text-sky-300 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/70"
                >
                  Review {armedFilters.length} filter{armedFilters.length === 1 ? '' : 's'}
                </button>
                {droppedCount > 0 && ` — ${droppedCount.toLocaleString()} dropped so far.`}
              </>
            ) : entries.length === 0 ? (
              isSent ? 'Nothing sent yet.' : 'Nothing received yet.'
            ) : (
              'Everything in this pane is hidden.'
            )}
          </EmptyState>
        ) : (
          <ul className="flex flex-col py-1">
            {visible.map((entry, index) => (
              <LogRow
                key={entry.seq}
                entry={entry}
                tone={tone}
                isSelected={selected?.seq === entry.seq}
                // Printed only when it CHANGES from the row above -- the log-file
                // convention. Fourteen rows repeating "DTU-Primary" held a third
                // of the pane's width for something that changes twice a screen,
                // and a change of owner now stands out instead of blending in.
                showConnection={index === 0 || visible[index - 1].connection_name !== entry.connection_name}
                delta={index === 0 ? null : formatDelta(entry.timestamp, visible[index - 1].timestamp)}
                dimmed={pairing !== null && !counterpartOf(pairing, entry)}
                onSelect={() => onSelect(entry)}
                onHover={onHover}
                onHide={() => setHidden((current) => new Set(current).add(entry.op_code))}
              />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function LogRow({
  entry, tone, isSelected, showConnection, delta, dimmed, onSelect, onHover, onHide,
}) {
  const isSent = tone === 'sky';

  return (
    <li
      onClick={onSelect}
      onMouseEnter={() => onHover?.(entry)}
      className={cx(
        'group animate-row-in flex cursor-pointer items-center gap-2 px-2 py-1',
        'border-l-2 transition-[background-color,border-color,opacity] duration-100',
        // The selected row used to take a sky border whatever pane it was in,
        // so selecting a RECEIVED message recoloured it to the sent hue at
        // exactly the moment it was most prominent. Selection now borrows the
        // pane's own tone; the background is what says "selected".
        isSelected
          ? isSent
            ? 'border-l-sky-500 bg-slate-800'
            : 'border-l-emerald-500 bg-slate-800'
          : cx(
              'border-l-transparent hover:bg-slate-800/50',
              isSent ? 'hover:border-l-sky-500/40' : 'hover:border-l-emerald-500/40',
            ),
        dimmed && 'opacity-30',
      )}
    >
      <span className="tnum shrink-0 font-mono text-[10px] text-slate-600">
        {formatTime(entry.timestamp)}
      </span>

      {/* The gap to the row above, which is the number you want when you are
          checking that a 0.5s behaviour really fires at 0.5s, or watching an
          echo timeout. An absolute clock cannot show it. */}
      <span className="tnum w-12 shrink-0 text-right font-mono text-[9px] text-slate-600">
        {delta ?? ''}
      </span>

      {/* Which GSim connection owns this entry -- needed now the console spans
          every connection rather than just the selected one, but only worth
          printing where it changes. */}
      {showConnection && (
        <span className="shrink-0 truncate font-mono text-[9px] text-slate-500">
          {entry.connection_name}
        </span>
      )}

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
