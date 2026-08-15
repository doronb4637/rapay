/**
 * Behaviour dialog: how a message should send itself.
 *
 * Opened from the Compose footer, and again from a row in the Behaviours panel
 * to edit one that already exists. There is at most ONE behaviour per
 * (connection, destination, opCode) route, so this is always an upsert -- the
 * dialog shows Stop/Remove instead of a second Create when the route already
 * has one.
 *
 * `KINDS` is the extension point: adding "burst" or "send N times" is a new
 * entry plus its own fields block, not a new dialog. The kind selector renders
 * even with one option so that shape is visible in the UI from the start.
 */
import React, { useState } from 'react';
import { AlertCircle, Loader2, Play, Repeat, Save, Square, Trash2, X } from 'lucide-react';
import { Badge, Button, Field, IconButton, Input, Select, cx } from './ui';

const KINDS = [
  {
    value: 'periodic',
    label: 'Periodic',
    icon: Repeat,
    blurb: 'Send this message over and over on a fixed interval.',
  },
];

//: Mirrors `MIN_INTERVAL_SECONDS` in core_gateway/behaviours.py. The server is
//: the real gate; this only turns a rejected request into an inline message.
const MIN_INTERVAL = 0.001;

export default function BehaviourModal({
  messageName, opCode, destination, existing, onSubmit, onStart, onStop, onDelete, onClose,
}) {
  const [kind, setKind] = useState(existing?.kind ?? 'periodic');
  const [interval, setInterval] = useState(String(existing?.interval ?? 1));
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const active = existing?.active;
  const enabled = existing?.enabled;
  const selectedKind = KINDS.find((entry) => entry.value === kind) ?? KINDS[0];

  const run = async (action) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      onClose();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const submit = (event) => {
    event.preventDefault();
    const seconds = Number(interval);
    if (!Number.isFinite(seconds) || seconds < MIN_INTERVAL) {
      return setError(`Interval must be a number of at least ${MIN_INTERVAL}s.`);
    }
    // Sends the payload currently in the compose form -- captured by the
    // caller, since this dialog deliberately does not duplicate the field
    // editor. Editing the message means editing it there and re-applying.
    run(() => onSubmit({ kind, interval: seconds }));
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-6 backdrop-blur-sm"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        onMouseDown={(event) => event.stopPropagation()}
        className="flex w-full max-w-md flex-col overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-2xl shadow-slate-950/70"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-slate-800 px-4 py-3">
          <Repeat size={14} className="text-sky-400" />
          <h2 className="text-sm font-semibold text-slate-100">Behaviour</h2>
          {existing && (
            <Badge tone={active ? 'emerald' : 'slate'}>
              {active ? 'running' : enabled ? 'armed' : 'stopped'}
            </Badge>
          )}
          <IconButton icon={X} title="Close" onClick={onClose} className="ml-auto" />
        </header>

        <form onSubmit={submit} className="flex flex-col gap-3 p-4">
          {/* What this behaviour is attached to. A behaviour belongs to one
              message on one link, so naming all three removes any doubt about
              which route the interval is about to change. */}
          <div className="flex flex-col gap-1 rounded-lg border border-slate-800 bg-slate-950/40 p-2.5">
            <div className="flex items-baseline gap-2">
              <span className="text-sm font-semibold text-slate-100">{messageName}</span>
              <Badge tone="sky">{`0x${Number(opCode).toString(16).toUpperCase().padStart(4, '0')}`}</Badge>
            </div>
            <span className="font-mono text-[10px] text-slate-500">to {destination}</span>
          </div>

          <Field label="Type">
            <Select value={kind} onChange={(event) => setKind(event.target.value)}>
              {KINDS.map((entry) => (
                <option key={entry.value} value={entry.value}>{entry.label}</option>
              ))}
            </Select>
          </Field>
          <p className="-mt-1 text-[10px] text-slate-500">{selectedKind.blurb}</p>

          {kind === 'periodic' && (
            <Field label="Interval (seconds)" hint={`minimum ${MIN_INTERVAL}s`}>
              <Input
                type="number" step="any" min={MIN_INTERVAL} value={interval} required
                onChange={(event) => setInterval(event.target.value)}
              />
            </Field>
          )}

          <p className="text-[10px] leading-snug text-slate-500">
            Sends the payload currently in the compose form. Every send is logged
            in the Sent console, exactly like pressing Send Message.
          </p>

          {existing && (
            <p className="text-[10px] leading-snug text-slate-500">
              This route already has a behaviour — saving replaces it and resets its counters.
            </p>
          )}

          {error && (
            <div className="flex items-start gap-2 rounded-lg border border-rose-900/60 bg-rose-950/30 p-2.5">
              <AlertCircle size={14} className="mt-px shrink-0 text-rose-400" />
              <pre className="min-w-0 whitespace-pre-wrap font-mono text-[11px] text-rose-300">{error}</pre>
            </div>
          )}
        </form>

        <footer className="flex shrink-0 items-center gap-2 border-t border-slate-800 bg-slate-900 px-4 py-3">
          {existing && (
            <>
              <IconButton
                icon={Trash2} title="Remove this behaviour" variant="danger"
                disabled={busy} onClick={() => run(() => onDelete(existing.id))}
              />
              <Button
                icon={enabled ? Square : Play}
                disabled={busy}
                onClick={() => run(() => (enabled ? onStop(existing.id) : onStart(existing.id)))}
              >
                {enabled ? 'Stop' : 'Start'}
              </Button>
            </>
          )}
          <Button onClick={onClose} className="ml-auto">Cancel</Button>
          <Button
            variant="primary" disabled={busy}
            icon={busy ? Loader2 : Save}
            className={cx(busy && '[&_svg]:animate-spin')}
            onClick={submit}
          >
            {existing ? 'Save changes' : 'Create behaviour'}
          </Button>
        </footer>
      </div>
    </div>
  );
}
