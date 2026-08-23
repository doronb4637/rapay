/**
 * A draggable divider between two panels.
 *
 * The three columns used to be hard-coded (`w-64`, flex, `w-[27rem]`) with no
 * way to change them, which is a strange thing to fix in place in a tool whose
 * two side panels are the parts people stare at for different reasons: someone
 * reading a busy console wants it wide, someone building a nested message wants
 * the workspace wide, and neither wanted the same thing ten minutes later.
 *
 * Keyboard-operable on purpose. A separator that only responds to a drag is one
 * more control that exists for mouse users only, and the arrow keys are the
 * obvious binding for a thing whose whole job is "a bit more this way".
 */
import React from 'react';
import { cx } from './ui';

const STEP = 16;

export default function Resizer({ value, min, max, onChange, side = 'left', label }) {
  const dragging = React.useRef(false);

  // Pointer capture rather than window listeners: the pointer keeps reporting
  // to this element even when it leaves it, which is exactly what a drag needs
  // and what a fast drag across the console would otherwise break.
  const onPointerDown = (event) => {
    dragging.current = true;
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event) => {
    if (!dragging.current) return;
    const next = side === 'left'
      ? event.clientX
      : window.innerWidth - event.clientX;
    onChange(Math.min(max, Math.max(min, Math.round(next))));
  };

  const onPointerUp = (event) => {
    dragging.current = false;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  };

  const nudge = (delta) => onChange(Math.min(max, Math.max(min, value + delta)));

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onDoubleClick={() => onChange(null)}
      onKeyDown={(event) => {
        // Grows the panel this separator belongs to, whichever side it is on,
        // so "right arrow widens the thing on the right" always holds.
        if (event.key === 'ArrowLeft') { event.preventDefault(); nudge(side === 'left' ? -STEP : STEP); }
        if (event.key === 'ArrowRight') { event.preventDefault(); nudge(side === 'left' ? STEP : -STEP); }
        if (event.key === 'Home') { event.preventDefault(); onChange(null); }
      }}
      title={`${label} — drag, or use the arrow keys. Double-click to reset.`}
      className={cx(
        'group relative z-10 w-1 shrink-0 cursor-col-resize touch-none bg-slate-800',
        'transition-colors hover:bg-sky-600 focus:outline-none focus-visible:bg-sky-500',
      )}
    >
      {/* A 1px target is unhittable, so the grab area is widened without moving
          anything: the visible line stays a hairline between panels. */}
      <span className="absolute inset-y-0 -left-1 -right-1 block" />
    </div>
  );
}
