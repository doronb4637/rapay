/**
 * The payload, byte by byte, under the message header.
 *
 * This is the only place in GSim that shows what a message actually looks like
 * on the wire -- which, for a simulator whose entire subject is a binary
 * protocol, it previously did not do anywhere. `LENGTH 29 B` was the whole of
 * it.
 *
 * It earns its space by being two-way: focusing a field lights its bytes, and
 * hovering a byte lights the field that owns it. That makes several things
 * visible that used to live only in docstrings and footnotes -- zero-fill (the
 * zeros are simply there), endianness, and a counted array's length byte
 * tracking its list as you add items.
 *
 * The framing header core prepends is deliberately NOT drawn. It is five bytes
 * this UI does not construct and cannot honestly label, and inventing a picture
 * of it would be worse than omitting it: the strip shows the payload, which is
 * exactly the thing the form controls.
 */
import React from 'react';
import { hexByte } from '../lib/bytes';
import { cx } from './ui';

/** Ten bytes to a group, so a long payload can be counted by eye instead of by
 *  reading every offset label. */
const GROUP = 10;

export default function ByteRuler({ bytes, leaves, activePath, onHoverPath }) {
  if (!bytes || bytes.length === 0) {
    return (
      <p className="font-mono text-[10px] text-slate-500">
        Empty payload — this message has no fields on the wire.
      </p>
    );
  }

  // Byte index -> the leaf that owns it, so hovering a byte can name its field
  // without searching the tree on every pointer move.
  const owner = new Array(bytes.length).fill(null);
  for (const leaf of leaves) {
    for (let i = leaf.offset; i < leaf.offset + leaf.size && i < bytes.length; i += 1) {
      owner[i] = leaf;
    }
  }

  const active = leaves.find((leaf) => leaf.path === activePath) ?? null;

  return (
    <div className="flex flex-col gap-1.5">
      <div className="overflow-x-auto pb-1">
        <div className="flex min-w-max gap-px">
          {Array.from(bytes).map((byte, index) => {
            const leaf = owner[index];
            const on = active !== null && leaf === active;
            const isGroupStart = index % GROUP === 0;
            return (
              <button
                key={index}
                type="button"
                tabIndex={-1}
                onMouseEnter={() => onHoverPath?.(leaf ? leaf.path : null)}
                onMouseLeave={() => onHoverPath?.(null)}
                title={leaf ? `${leaf.path} — byte ${index}` : `byte ${index}`}
                className={cx(
                  'tnum w-[26px] shrink-0 rounded-[3px] px-0 py-0.5 text-center font-mono',
                  'text-[10px] leading-tight transition-colors duration-75',
                  isGroupStart && index > 0 && 'ml-1.5',
                  on
                    ? 'bg-sky-500/20 text-sky-200 ring-1 ring-inset ring-sky-500/60'
                    : 'bg-slate-950/60 text-slate-400 hover:bg-slate-800',
                )}
              >
                {hexByte(byte)}
                <span
                  className={cx(
                    'block text-[8px]',
                    on ? 'text-sky-300' : 'text-slate-600',
                  )}
                >
                  {index}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* One line, always present, so the strip never changes height as the
          pointer moves across it. */}
      <p className="truncate font-mono text-[10px] text-slate-500">
        {active ? (
          <>
            <span className="font-semibold text-sky-300">{active.path}</span>{' '}
            {active.size === 1
              ? `byte ${active.offset}`
              : `bytes ${active.offset}–${active.offset + active.size - 1}`}
            {active.size > 1 && `, ${active.node.endian ?? 'little'}-endian`}
            {active.node.dtype && ` · ${active.node.dtype}`}
          </>
        ) : (
          `${bytes.length} B payload — hover a field or a byte`
        )}
      </p>
    </div>
  );
}
