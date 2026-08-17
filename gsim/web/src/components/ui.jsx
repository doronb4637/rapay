/**
 * Shared UI primitives.
 *
 * The design system lives here rather than being re-typed as class strings in
 * every panel: one definition of what a button, input or panel looks like, so
 * the surfaces stay consistent and a token change is a one-line edit.
 *
 * Palette: slate-950 app ground, slate-900 panels, slate-800 raised/hover,
 * slate-700/800 borders. Sky is the interactive accent (focus, primary
 * actions); emerald means "live" (running connections, inbound messages).
 */
import React from 'react';

export function cx(...parts) {
  return parts.filter(Boolean).join(' ');
}

/* ------------------------------------------------------------------ */
/* Surfaces                                                            */
/* ------------------------------------------------------------------ */
export function Panel({ className, children, ...rest }) {
  return (
    <section
      className={cx('flex min-h-0 flex-col bg-slate-900', className)}
      {...rest}
    >
      {children}
    </section>
  );
}

export function PanelHeader({ title, icon: Icon, count, children, className }) {
  return (
    <header
      className={cx(
        'flex h-9 shrink-0 items-center gap-2 border-b border-slate-800 px-3',
        className,
      )}
    >
      {Icon && <Icon size={14} className="text-slate-500" />}
      <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
        {title}
      </h2>
      {count !== undefined && (
        <span className="rounded bg-slate-800 px-1.5 py-px font-mono text-[10px] text-slate-400">
          {count}
        </span>
      )}
      <div className="ml-auto flex items-center gap-1">{children}</div>
    </header>
  );
}

/* ------------------------------------------------------------------ */
/* Buttons                                                             */
/* ------------------------------------------------------------------ */
const BUTTON_VARIANTS = {
  default:
    'bg-slate-800 text-slate-200 hover:bg-slate-700 border border-slate-700 hover:border-slate-600',
  primary:
    'bg-sky-600 text-white hover:bg-sky-500 border border-sky-500/60 shadow-sm shadow-sky-950/40',
  ghost: 'text-slate-400 hover:text-slate-100 hover:bg-slate-800 border border-transparent',
  danger:
    'bg-slate-800 text-rose-300 hover:bg-rose-950/60 hover:text-rose-200 border border-slate-700 hover:border-rose-900',
};

export function Button({ variant = 'default', icon: Icon, className, children, ...rest }) {
  return (
    <button
      type="button"
      className={cx(
        'inline-flex items-center justify-center gap-1.5 rounded-md px-2.5 py-1.5',
        'text-xs font-medium transition-colors duration-100',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/70',
        'disabled:pointer-events-none disabled:opacity-40',
        BUTTON_VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {Icon && <Icon size={14} strokeWidth={2} />}
      {children}
    </button>
  );
}

/** Square icon-only button. `title` doubles as the accessible label. */
export function IconButton({ icon: Icon, title, variant = 'ghost', className, ...rest }) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      className={cx(
        'inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors duration-100',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/70',
        'disabled:pointer-events-none disabled:opacity-30',
        BUTTON_VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      <Icon size={15} strokeWidth={2} />
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* Form controls                                                       */
/* ------------------------------------------------------------------ */
const CONTROL = cx(
  'w-full rounded-md border border-slate-700 bg-slate-950/60 px-2.5 py-1.5',
  'text-xs text-slate-100 placeholder:text-slate-600',
  'transition-colors duration-100',
  'hover:border-slate-600',
  'focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500/40',
  'disabled:cursor-not-allowed disabled:bg-slate-900 disabled:text-slate-500',
);

export function Input({ className, ...rest }) {
  return <input className={cx(CONTROL, 'font-mono', className)} {...rest} />;
}

/**
 * An Input for filesystem paths, abbreviated while idle.
 *
 * A browsed structures path is long and front-loaded with the least useful part
 * (`C:\Projects\Rapat\rapay\core\IRS\...`), so in a narrow field the only thing
 * visible is the part that never varies. This shows the tail while the field is
 * idle and the REAL value the moment it is focused -- so clicking in, Ctrl+A and
 * copy all yield the complete path, and the value handed to the server is
 * always untouched.
 *
 * Deliberately not a formatting of the value itself: the path is what core
 * resolves and what a saved config stores, so it must survive a round trip
 * through this field byte for byte.
 */
export function PathInput({ value, className, onFocus, onBlur, ...rest }) {
  const [focused, setFocused] = React.useState(false);
  const text = String(value ?? '');
  return (
    <input
      className={cx(CONTROL, 'font-mono', className)}
      // `title` covers the hover case; focus covers select/copy.
      title={text || undefined}
      value={focused ? text : abbreviatePath(text)}
      onFocus={(event) => { setFocused(true); onFocus?.(event); }}
      onBlur={(event) => { setFocused(false); onBlur?.(event); }}
      {...rest}
    />
  );
}

/** `…\Structures\Tiful\tiful_to_dtu.py` -- the last few segments, which are the
 *  ones that identify the file. Left alone if it is already short, or if it is
 *  a dotted module name rather than a path. */
export function abbreviatePath(text, segments = 3) {
  if (!text || text.length <= 40) return text;
  const separator = text.includes('\\') ? '\\' : '/';
  const parts = text.split(separator).filter(Boolean);
  if (parts.length <= segments) return text;
  return `…${separator}${parts.slice(-segments).join(separator)}`;
}

export function Select({ className, children, ...rest }) {
  return (
    <select className={cx(CONTROL, 'cursor-pointer appearance-none pr-7', className)} {...rest}>
      {children}
    </select>
  );
}

/** Label + control + optional hint, in the standard vertical rhythm. */
export function Field({ label, type, hint, htmlFor, children, className }) {
  return (
    <div className={cx('flex min-w-0 flex-1 flex-col gap-1', className)}>
      {label && (
        <label
          htmlFor={htmlFor}
          className="flex items-baseline gap-1.5 text-[11px] font-medium text-slate-300"
        >
          {label}
          {type && <span className="font-mono text-[9px] uppercase text-slate-500">{type}</span>}
        </label>
      )}
      {children}
      {hint && <span className="text-[10px] text-slate-500">{hint}</span>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Indicators                                                          */
/* ------------------------------------------------------------------ */
export function StatusDot({ on, className }) {
  return (
    <span
      className={cx(
        'inline-block h-2 w-2 shrink-0 rounded-full transition-colors',
        on ? 'bg-emerald-400 shadow-[0_0_6px] shadow-emerald-400/70' : 'bg-slate-600',
        className,
      )}
    />
  );
}

const BADGE_TONES = {
  slate: 'bg-slate-800 text-slate-300 ring-slate-700',
  sky: 'bg-sky-500/15 text-sky-300 ring-sky-500/30',
  emerald: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30',
  rose: 'bg-rose-500/15 text-rose-300 ring-rose-500/30',
  amber: 'bg-amber-500/15 text-amber-300 ring-amber-500/30',
};

export function Badge({ tone = 'slate', className, children }) {
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 rounded px-1.5 py-px',
        'font-mono text-[10px] font-medium ring-1 ring-inset',
        BADGE_TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function EmptyState({ icon: Icon, children }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
      {Icon && <Icon size={22} className="text-slate-700" />}
      <p className="max-w-[22rem] text-xs text-slate-500">{children}</p>
    </div>
  );
}
