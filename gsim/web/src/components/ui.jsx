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

/**
 * UDP's "server"/"client" names its socket role, not the traffic direction --
 * confusing here specifically because the roles don't map to intuition.
 * Confirmed against `core/connections/udp.py`: a client has `remote_addr`
 * set at `_do_start` and can send immediately, while a server only learns a
 * peer address from its first inbound datagram (`_remember_peer`) -- so the
 * client is the one that sends first and the server the one that starts out
 * waiting to receive. Everywhere UDP's side is displayed, it reads as what
 * it does (Sender/Receiver) with the socket role kept alongside for anyone
 * who already thinks in those terms. TCP and DDS keep their own vocabulary
 * (client/server, publisher/subscriber) unchanged -- this relabeling is
 * UDP-specific.
 */
const UDP_SIDE_LABELS = { client: 'Sender (Client)', server: 'Receiver (Server)' };
export function sideLabel(protocol, side) {
  if (protocol === 'udp') return UDP_SIDE_LABELS[side] ?? side;
  return side;
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

/**
 * `rank` is the difference between the panel you are working in and the panels
 * you are picking things out of. Every header used to be the same 11px
 * uppercase slate-400 -- Connections, Messages, Behaviours, Compose, Sent and
 * Received all reading as equals -- so nothing in the type said where the
 * workspace was, and the empty centre read as a void rather than as the panel
 * waiting for input.
 */
const HEADER_RANKS = {
  rail: 'text-[11px] text-slate-400',
  workspace: 'text-[12px] text-slate-200',
};

export function PanelHeader({ title, icon: Icon, count, children, className, rank = 'rail' }) {
  return (
    <header
      className={cx(
        'flex h-9 shrink-0 items-center gap-2 border-b border-slate-800 px-3',
        className,
      )}
    >
      {Icon && <Icon size={14} className={rank === 'workspace' ? 'text-slate-400' : 'text-slate-500'} />}
      <h2 className={cx('font-semibold uppercase tracking-wider', HEADER_RANKS[rank])}>
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
        // `dot-live` (styles.css) is a halo in dark and a ring in light. The
        // glow alone was invisible on white, which left an 8px hue difference
        // doing the whole job for the most-scanned state in the app.
        on ? 'bg-emerald-400 dot-live' : 'bg-slate-600',
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

/* ------------------------------------------------------------------ */
/* Dense field row                                                     */
/* ------------------------------------------------------------------ */
/**
 * Label on the left, control on the right, sized to what the value can hold.
 *
 * `Field` above stacks its label over a full-width control, which is right for
 * the connection modal (long free text, paths, IP addresses) and wrong for a
 * message form, where almost every value is a small number with a known
 * ceiling. Rendering those the same way gave a `UInt8` the same ~900px box as a
 * `Double` and turned a seven-field message into something that scrolled while
 * holding seven zeros.
 *
 * `chars` comes from the schema (`controlChars` in lib/schema.js), so the width
 * is derived rather than guessed, and `1fr` on the label column means the two
 * sides still meet however long the field names are.
 */
export function FieldRow({
  label, type, hint, htmlFor, chars = 10, children, onPointerEnter, onPointerLeave, active,
}) {
  return (
    <div
      onMouseEnter={onPointerEnter}
      onMouseLeave={onPointerLeave}
      className={cx(
        'grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 rounded-md px-1.5 py-[3px]',
        'transition-colors duration-75',
        active ? 'bg-sky-500/10' : 'hover:bg-slate-800/40',
      )}
    >
      <label
        htmlFor={htmlFor}
        className="flex min-w-0 items-baseline gap-1.5 text-[11px] font-medium text-slate-300"
      >
        {/* The field NAME is the thing that must survive a narrow column, so
            the type and hint beside it give up space several times faster --
            without this the two-column layout clipped "TrackQuality" to
            "Track..." while its `QUALITYLEVEL` badge sat there intact. */}
        <span className="truncate">{label}</span>
        {type && (
          <span className="shrink-[6] truncate font-mono text-[9px] uppercase text-slate-500">
            {type}
          </span>
        )}
        {hint && (
          <span className="shrink-[6] truncate text-[10px] font-normal text-slate-500">{hint}</span>
        )}
      </label>
      <div style={{ width: `${chars}ch` }} className="min-w-0">
        {children}
      </div>
    </div>
  );
}

/**
 * A read-only field's value, as data rather than as a disabled input.
 *
 * Inspecting a received message used to render the compose form with
 * `disabled` on every control: greyed-out text boxes, and an enum that kept its
 * dropdown chevron -- an affordance promising something that could not happen.
 * A decode is a read-out, so it reads as one: right-aligned, tabular, with the
 * hex alongside because this is a hex protocol and that is the form the log
 * rows and the IRS documents both use.
 */
export function ValueCell({ text, hex: hexText, title }) {
  return (
    <div
      title={title}
      className="flex items-baseline justify-end gap-2 truncate font-mono text-[11px] text-slate-100"
    >
      {hexText && <span className="shrink-0 text-[10px] text-slate-500">{hexText}</span>}
      <span className="tnum truncate">{text}</span>
    </div>
  );
}
