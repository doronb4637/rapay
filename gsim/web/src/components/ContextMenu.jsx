/**
 * Desktop-style right-click menu.
 *
 * Driven by an `items` array rather than children, so a caller adds an option
 * by adding an object -- the console starts with just "Clear" and is expected
 * to grow (copy, export, filter by opCode, ...) without this component
 * changing at all.
 *
 * Item shape:
 *   { label, onSelect, icon?, danger?, disabled?, hint? }   an entry
 *   { separator: true }                                     a divider rule
 *
 * Positioning is fixed-viewport and flipped near the edges, because the menu is
 * opened at the pointer: near the bottom or right of the window an unflipped
 * menu would render partly offscreen with no way to reach its last item.
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { cx } from './ui';

const MENU_WIDTH = 176;

export default function ContextMenu({ x, y, items, onClose }) {
  const menuRef = useRef(null);
  const [position, setPosition] = useState({ left: x, top: y });

  // Measure after mount, before paint: the height depends on how many items
  // the caller passed, so the flip decision cannot be made from x/y alone.
  useLayoutEffect(() => {
    const element = menuRef.current;
    if (!element) return;
    const { width, height } = element.getBoundingClientRect();
    setPosition({
      left: x + width > window.innerWidth ? Math.max(4, x - width) : x,
      top: y + height > window.innerHeight ? Math.max(4, y - height) : y,
    });
  }, [x, y]);

  // A click/right-click OUTSIDE the menu, any scroll, Escape, or a resize
  // dismisses it -- matching what every desktop menu does.
  //
  // Deliberately checks containment via the ref rather than closing
  // unconditionally on capture and relying on the menu's own `stopPropagation`
  // to save a click on one of its items: capture-phase listeners on `window`
  // run top-down, BEFORE the event ever reaches the target, so a bubble-phase
  // `stopPropagation` on the menu is always too late to stop them -- it fires
  // after the ancestor listener already did. That ordering silently ate every
  // menu click: `close()` unmounted the menu between `mousedown` and `click`,
  // so the item's `onClick` (and whatever API call it made) never ran. A real
  // click reliably hit this; a synthetic `.click()` in a test does not, since
  // it fires `click` directly and skips `mousedown` -- which is exactly why
  // this passed every automated check and failed for every real user.
  useEffect(() => {
    const dismissIfOutside = (event) => {
      if (menuRef.current?.contains(event.target)) return;
      onClose();
    };
    const onKey = (event) => event.key === 'Escape' && onClose();
    window.addEventListener('mousedown', dismissIfOutside, true);
    window.addEventListener('contextmenu', dismissIfOutside, true);
    window.addEventListener('wheel', onClose, true);
    window.addEventListener('resize', onClose);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', dismissIfOutside, true);
      window.removeEventListener('contextmenu', dismissIfOutside, true);
      window.removeEventListener('wheel', onClose, true);
      window.removeEventListener('resize', onClose);
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  return (
    <div
      ref={menuRef}
      role="menu"
      style={{ left: position.left, top: position.top, minWidth: MENU_WIDTH }}
      // Blocks the OS/browser context menu when right-clicking ON our menu
      // (e.g. right-clicking a menu item). Dismissal itself is handled by the
      // ref-containment check above, not by stopping propagation here.
      onContextMenu={(event) => event.preventDefault()}
      className={cx(
        'fixed z-[60] overflow-hidden rounded-md border border-slate-700',
        'bg-slate-900 py-1 shadow-xl shadow-slate-950/80',
      )}
    >
      {items.map((item, index) =>
        item.separator ? (
          <div key={`sep-${index}`} className="my-1 h-px bg-slate-800" />
        ) : (
          <button
            key={item.label}
            type="button"
            role="menuitem"
            disabled={item.disabled}
            onClick={() => {
              onClose();
              item.onSelect?.();
            }}
            className={cx(
              'flex w-full items-center gap-2 px-3 py-1.5 text-left text-[11px]',
              'transition-colors duration-75',
              'disabled:pointer-events-none disabled:text-slate-600',
              item.danger
                ? 'text-rose-300 hover:bg-rose-950/50 hover:text-rose-200'
                : 'text-slate-200 hover:bg-slate-800',
            )}
          >
            {item.icon && <item.icon size={12} className="shrink-0" />}
            <span className="flex-1">{item.label}</span>
            {item.hint && <span className="font-mono text-[10px] text-slate-600">{item.hint}</span>}
          </button>
        ),
      )}
    </div>
  );
}

/**
 * State + handlers for one context-menu host.
 *
 * Returns `{ menuProps, open }`: spread nothing, render `<ContextMenu
 * {...menuProps} items={...} />` when `menuProps` is non-null, and wire `open`
 * to `onContextMenu`. Kept as a hook so several panels can each own a menu
 * without duplicating the open/close/position bookkeeping.
 */
export function useContextMenu() {
  const [anchor, setAnchor] = useState(null);
  const open = (event) => {
    event.preventDefault();
    setAnchor({ x: event.clientX, y: event.clientY });
  };
  const close = () => setAnchor(null);
  return {
    isOpen: anchor !== null,
    open,
    close,
    menuProps: anchor && { x: anchor.x, y: anchor.y, onClose: close },
  };
}
