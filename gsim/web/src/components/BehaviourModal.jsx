/**
 * Behaviour dialog: what this simulated unit sends, and what makes it send.
 *
 * A behaviour is a conditional rule, so the dialog is laid out as one: a
 * **WHEN** block naming the stimulus and a **THEN** block naming the response.
 * Those two words are the structural device because they are the actual grammar
 * of the thing being configured -- not `01 / 02 / 03` markers, which would imply
 * a sequence that is not there.
 *
 * The two blocks borrow the console's own colour coding rather than inventing
 * one: **emerald is inbound** everywhere in this app (the Received pane, a live
 * peer), and every trigger is an arrival; **sky is outbound** (the Sent pane,
 * primary actions), and every action is a send. So the dialog reads against the
 * console behind it. Amber is the third: it already means "held back" here (the
 * console's `paused` chip, a behaviour short of its rate), which is exactly what
 * a condition and a delay do to a response. Rose stays absent -- nothing in this
 * dialog is an error.
 *
 * Typography follows `FilterModal`: **mono is what exists on the wire** (message
 * names, field paths, opcodes, values), sans is the grammar the user wrote
 * (`only if`, `wait`, `every`). In one face you cannot tell the field named
 * `Len` from the word "len".
 *
 * The mapping rows carry the **last value that actually travelled them**. That
 * is the one memorable thing here and it is not decoration: a mapping silently
 * reading a field that is not there looks identical to a working one until it
 * shows you a number, and "is my handshake really echoing the transaction id?"
 * is the question this feature exists to answer.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  AlertCircle, ArrowDownLeft, ArrowRight, ArrowUpRight, Loader2, Play, Plus,
  Save, Square, Trash2, X, Zap,
} from 'lucide-react';
import { api } from '../api';
import { Badge, Button, Field, IconButton, Input, Select, cx } from './ui';
import { fieldTargets, operatorsFor, valueControl } from '../lib/fieldTargets';

//: Mirrors `MIN_INTERVAL_SECONDS` in core_gateway/behaviours.py. The server is
//: the real gate; this only turns a rejected request into an inline message.
const MIN_INTERVAL = 0.001;
//: Mirrors `MAX_DELAY_MS`. A latency simulates processing time; beyond this it
//: is a schedule wearing a disguise.
const MAX_DELAY_MS = 60000;

/** The stimulus. `blurb` is what the row says when it is the chosen one. */
const TRIGGERS = [
  {
    value: 'immediate',
    label: 'periodic sending',
    blurb: 'Sends on a fixed interval as soon as the behaviour and its connection are both running.',
  },
  {
    value: 'on_connect',
    label: 'the unit connects',
    blurb: 'Fires the moment the peer has a usable link — a handshake or an init burst.',
  },
  {
    value: 'on_received',
    label: 'a message arrives',
    blurb: 'Fires on each matching message from the peer.',
  },
];

const emptyDraft = (existing, destination) => ({
  trigger: existing?.trigger ?? 'immediate',
  mode: existing?.mode ?? 'periodic',
  interval: String(existing?.interval ?? 1),
  delayMs: String(existing?.delay_ms ?? 0),
  triggerUnitName: existing?.trigger_unit_name ?? destination ?? null,
  triggerOpCode: existing?.trigger_op_code ?? null,
  condition: existing?.condition ? { ...existing.condition } : null,
  mappings: (existing?.mappings ?? []).map((entry) => ({ ...entry })),
});

export default function BehaviourModal({
  messageName, opCode, destination, connectionName, peers = [], existing,
  onSubmit, onStart, onStop, onDelete, onClose,
}) {
  const [draft, setDraft] = useState(() => emptyDraft(existing, destination));
  const [incoming, setIncoming] = useState([]);       // messages the peer can send
  const [sourceTargets, setSourceTargets] = useState([]);   // fields of the incoming one
  const [outTargets, setOutTargets] = useState([]);         // fields of what we send
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const active = existing?.active;
  const enabled = existing?.enabled;
  const reactive = draft.trigger === 'on_received';
  // 'periodic sending' has no event to react to, so a single firing would
  // never repeat on its own -- the trigger IS the schedule, and 'send once'
  // is not a meaningful choice under it.
  const isImmediate = draft.trigger === 'immediate';
  // The brief's constraint: with one peer there is nothing to choose, so the
  // control is not rendered at all rather than rendered with a single option.
  const onePeer = peers.length <= 1;
  const triggerUnit = draft.triggerUnitName ?? destination;

  /* What the peer can send us — the trigger picker's options. Same endpoint the
     filter dialog uses; it lists from the registry rather than from what has
     already arrived, so a rule can be written before the first message. */
  useEffect(() => {
    if (!connectionName || !reactive) return undefined;
    let cancelled = false;
    api.incoming(connectionName).then(
      (rows) => !cancelled && setIncoming(rows),
      (err) => !cancelled && setError(err.message),
    );
    return () => { cancelled = true; };
  }, [connectionName, reactive]);

  const forUnit = useMemo(
    () => incoming.filter((row) => row.unit_name === triggerUnit),
    [incoming, triggerUnit],
  );

  // Default the watched message to the first one the peer can send, so the
  // trigger is never half-configured just because nothing was picked.
  useEffect(() => {
    if (!reactive || draft.triggerOpCode !== null || forUnit.length === 0) return;
    setDraft((current) => ({ ...current, triggerOpCode: forUnit[0].op_code }));
  }, [reactive, forUnit, draft.triggerOpCode]);

  /* The INCOMING message's fields, fetched by the SENDER's unit code — a
     received message is decoded under the peer's code, so asking under ours
     would find the wrong layout or none. */
  const watched = forUnit.find((row) => row.op_code === draft.triggerOpCode) ?? null;
  useEffect(() => {
    if (!watched) { setSourceTargets([]); return undefined; }
    let cancelled = false;
    api.schemaByUnit(watched.unit_code, watched.op_code, watched.namespace).then(
      (schema) => !cancelled && setSourceTargets(fieldTargets(schema)),
      (err) => !cancelled && setError(err.message),
    );
    return () => { cancelled = true; };
  }, [watched?.unit_code, watched?.op_code, watched?.namespace]);

  /* The OUTGOING message's fields — the only valid mapping targets. */
  useEffect(() => {
    if (!connectionName || opCode === undefined) return undefined;
    let cancelled = false;
    api.messageSchema(connectionName, opCode, destination).then(
      (schema) => !cancelled && setOutTargets(fieldTargets(schema)),
      () => {},
    );
    return () => { cancelled = true; };
  }, [connectionName, opCode, destination]);

  const sourceFields = sourceTargets.filter((entry) => entry.ruleOk);
  const outFields = outTargets.filter((entry) => entry.ruleOk);
  const conditionTarget = draft.condition
    && sourceFields.find((entry) => entry.path === draft.condition.path);

  const run = async (action) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      onClose();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  };

  const submit = (event) => {
    event?.preventDefault();
    const seconds = Number(draft.interval);
    if (draft.mode === 'periodic' && (!Number.isFinite(seconds) || seconds < MIN_INTERVAL)) {
      return setError(`Interval must be a number of at least ${MIN_INTERVAL}s.`);
    }
    const delay = Number(draft.delayMs || 0);
    if (!Number.isFinite(delay) || delay < 0 || delay > MAX_DELAY_MS) {
      return setError(`Delay must be between 0 and ${MAX_DELAY_MS}ms.`);
    }
    return run(() => onSubmit({
      trigger: draft.trigger,
      mode: draft.mode,
      interval: seconds,
      delay_ms: delay,
      trigger_unit_name: reactive ? triggerUnit : null,
      trigger_op_code: reactive ? draft.triggerOpCode : null,
      condition: reactive && draft.condition ? draft.condition : null,
      mappings: reactive ? draft.mappings.filter((m) => m.from && m.to) : [],
    }));
  };

  const addCondition = () => {
    const target = sourceFields[0];
    if (!target) return;
    setDraft((current) => ({
      ...current,
      condition: {
        path: target.path,
        op: '==',
        value: target.kind === 'enum' ? (target.options?.[0]?.name ?? null) : 0,
      },
    }));
  };

  const editCondition = (patch) => setDraft((current) => {
    const next = { ...current.condition, ...patch };
    if (patch.path !== undefined) {
      // Retargeting can invalidate the operator and the value, so both are
      // re-seeded from the new field rather than left to fail in the PUT.
      const target = sourceFields.find((entry) => entry.path === patch.path);
      const allowed = operatorsFor(target).map((operator) => operator.value);
      if (!allowed.includes(next.op)) next.op = '==';
      next.value = target?.kind === 'enum' ? (target.options?.[0]?.name ?? null) : 0;
    }
    return { ...current, condition: next };
  });

  const addMapping = () => {
    if (!sourceFields.length || !outFields.length) return;
    // Seed with a pair of the SAME kind where one exists: a scalar cannot be
    // copied into an enum (one travels as a number, the other as a member
    // name), and the server refuses it — better not to offer it as the default.
    const source = sourceFields[0];
    const target = outFields.find((entry) => entry.kind === source.kind) ?? outFields[0];
    setDraft((current) => ({
      ...current,
      mappings: [...current.mappings, { from: source.path, to: target.path }],
    }));
  };

  const editMapping = (index, patch) => setDraft((current) => ({
    ...current,
    mappings: current.mappings.map((entry, at) => (at === index ? { ...entry, ...patch } : entry)),
  }));

  const removeMapping = (index) => setDraft((current) => ({
    ...current, mappings: current.mappings.filter((_, at) => at !== index),
  }));

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-6 backdrop-blur-sm"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        onMouseDown={(event) => event.stopPropagation()}
        className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-2xl shadow-slate-950/70"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-slate-800 px-4 py-3">
          <Zap size={14} className="text-sky-400" />
          <h2 className="text-sm font-semibold text-slate-100">Behaviour</h2>
          <span className="ml-1 font-mono text-[11px] text-slate-400">{messageName}</span>
          <Badge tone="sky">
            {`0x${Number(opCode).toString(16).toUpperCase().padStart(4, '0')}`}
          </Badge>
          <span className="font-mono text-[10px] text-slate-500">→ {destination}</span>
          {existing && (
            <Badge tone={active ? 'emerald' : 'slate'} className="ml-1">
              {active ? 'running' : enabled ? 'armed' : 'stopped'}
            </Badge>
          )}
          <IconButton icon={X} title="Close" onClick={onClose} className="ml-auto" />
        </header>

        <form onSubmit={submit} className="min-h-0 flex-1 overflow-y-auto p-4">
          {/* ------------------------------------------------------- WHEN */}
          <div className="flex items-center gap-2">
            <ArrowDownLeft size={12} className="text-emerald-400" />
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
              When
            </h3>
          </div>
          <div className="mt-1.5 flex flex-col gap-0.5" role="radiogroup" aria-label="When">
            {TRIGGERS.map((entry) => {
              const on = draft.trigger === entry.value;
              return (
                <label
                  key={entry.value}
                  className={cx(
                    'flex cursor-pointer flex-wrap items-center gap-2 rounded-md px-2 py-1.5',
                    'transition-colors duration-100',
                    on
                      ? 'bg-emerald-500/10 ring-1 ring-inset ring-emerald-500/25'
                      : 'hover:bg-slate-800/50',
                  )}
                >
                  <input
                    type="radio"
                    name="behaviour-trigger"
                    className="accent-emerald-500"
                    checked={on}
                    onChange={() => setDraft((current) => ({
                      ...current,
                      trigger: entry.value,
                      // A condition and a mapping both read the incoming
                      // message; leaving them attached to a trigger that has
                      // none would be configuring something that cannot run.
                      condition: entry.value === 'on_received' ? current.condition : null,
                      mappings: entry.value === 'on_received' ? current.mappings : [],
                      // 'periodic sending' forces the schedule -- there is
                      // nothing else it could mean. The other two default to
                      // a single reply, which is the ordinary request/response
                      // shape; picking 'periodic sending' there is a deliberate
                      // second step, not the first thing tried.
                      mode: entry.value === 'immediate' ? 'periodic' : 'once',
                    }))}
                  />
                  <span className={cx('text-[11px]', on ? 'text-slate-100' : 'text-slate-400')}>
                    {entry.value === 'immediate' ? entry.label : (
                      <>
                        {onePeer
                          ? <span className="font-mono text-[11px]">{triggerUnit}</span>
                          : null}
                        {onePeer ? ' ' : ''}
                        {entry.value === 'on_connect' ? (onePeer ? 'connects' : entry.label)
                          : (onePeer ? 'sends' : entry.label)}
                      </>
                    )}
                  </span>

                  {/* The unit picker only appears when there is a choice. */}
                  {on && entry.value !== 'immediate' && !onePeer && (
                    <Select
                      value={triggerUnit ?? ''}
                      onChange={(event) => setDraft((current) => ({
                        ...current,
                        triggerUnitName: event.target.value,
                        triggerOpCode: null,
                        condition: null,
                        mappings: [],
                      }))}
                      onClick={(event) => event.preventDefault()}
                      className="!w-auto !py-0.5 !text-[10px]"
                    >
                      {peers.map((peer) => (
                        <option key={peer.name} value={peer.name}>{peer.name}</option>
                      ))}
                    </Select>
                  )}

                  {on && entry.value === 'on_received' && (
                    <Select
                      value={draft.triggerOpCode ?? ''}
                      onChange={(event) => setDraft((current) => ({
                        ...current,
                        triggerOpCode: Number(event.target.value),
                        condition: null,
                        mappings: [],
                      }))}
                      onClick={(event) => event.preventDefault()}
                      className="!w-auto !py-0.5 !text-[10px]"
                    >
                      {forUnit.length === 0 && <option value="">no messages</option>}
                      {forUnit.map((row) => (
                        <option key={row.op_code} value={row.op_code}>
                          {row.name} · {row.op_code_hex}
                        </option>
                      ))}
                    </Select>
                  )}
                </label>
              );
            })}
          </div>
          <p className="mt-1 pl-2 text-[10px] leading-snug text-slate-500">
            {TRIGGERS.find((entry) => entry.value === draft.trigger)?.blurb}
          </p>

          {/* Condition — amber, because it withholds the response. */}
          {reactive && (
            <div className="mt-2 pl-2">
              {draft.condition ? (
                <div className="animate-row-in flex flex-wrap items-center gap-1.5 rounded-md bg-amber-500/10 px-2 py-1.5 ring-1 ring-inset ring-amber-500/25">
                  <span className="shrink-0 text-[10px] text-slate-400">only if</span>
                  <Select
                    value={draft.condition.path}
                    onChange={(event) => editCondition({ path: event.target.value })}
                    className="!w-auto !min-w-0 !flex-1 !py-0.5 !text-[10px]"
                  >
                    {sourceFields.map((entry) => (
                      <option key={entry.path} value={entry.path}>{entry.path}</option>
                    ))}
                  </Select>
                  <Select
                    value={draft.condition.op}
                    onChange={(event) => editCondition({ op: event.target.value })}
                    className="!w-[4.5rem] !py-0.5 !text-[10px]"
                  >
                    {operatorsFor(conditionTarget).map((operator) => (
                      <option key={operator.value} value={operator.value}>{operator.label}</option>
                    ))}
                  </Select>
                  {valueControl(conditionTarget).type === 'enum' ? (
                    <Select
                      value={draft.condition.value ?? ''}
                      onChange={(event) => editCondition({ value: event.target.value })}
                      className="!w-28 !py-0.5 !text-[10px]"
                    >
                      {(conditionTarget?.options ?? []).map((option) => (
                        <option key={option.name} value={option.name}>{option.name}</option>
                      ))}
                    </Select>
                  ) : (
                    <Input
                      type="number"
                      value={draft.condition.value ?? ''}
                      onChange={(event) => editCondition({
                        value: event.target.value === '' ? '' : Number(event.target.value),
                      })}
                      className="!w-20 !py-0.5 !text-[10px]"
                    />
                  )}
                  {existing?.rejected_count > 0 && (
                    <span
                      className="tnum shrink-0 font-mono text-[10px] text-amber-500/90"
                      title={`${existing.rejected_count} arrivals did not match`}
                    >
                      {existing.rejected_count.toLocaleString()} skipped
                    </span>
                  )}
                  <IconButton
                    icon={Trash2} title="Remove this condition" variant="danger"
                    className="!h-5 !w-5 shrink-0"
                    onClick={() => setDraft((current) => ({ ...current, condition: null }))}
                  />
                </div>
              ) : (
                <Button
                  icon={Plus} className="!py-1"
                  disabled={sourceFields.length === 0}
                  title={sourceFields.length === 0
                    ? 'This message has no field holding a single value to compare'
                    : 'Only respond when a field matches'}
                  onClick={addCondition}
                >
                  Add condition
                </Button>
              )}
            </div>
          )}

          {/* ------------------------------------------------------- THEN */}
          <div className="mt-4 flex items-center gap-2">
            <ArrowUpRight size={12} className="text-sky-400" />
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
              Then
            </h3>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md bg-sky-500/10 px-2.5 py-2 ring-1 ring-inset ring-sky-500/25">
            {reactive && (
              <label className="flex items-center gap-1.5">
                <span className="text-[10px] text-slate-400">wait</span>
                <Input
                  type="number" min={0} max={MAX_DELAY_MS} value={draft.delayMs}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, delayMs: event.target.value }))}
                  className="!w-16 !py-0.5 !text-[10px]"
                />
                <span className="text-[10px] text-slate-400">ms, then</span>
              </label>
            )}
            {/* 'periodic sending' forces the schedule -- there is nothing else
                it could mean under a trigger that never repeats on its own --
                so 'send once' is not offered as a choice at all under it. */}
            {!isImmediate && (
              <label className="flex cursor-pointer items-center gap-1.5">
                <input
                  type="radio" name="behaviour-mode" className="accent-sky-500"
                  checked={draft.mode === 'once'}
                  onChange={() => setDraft((current) => ({ ...current, mode: 'once' }))}
                />
                <span className="text-[11px] text-slate-200">send once</span>
              </label>
            )}
            <label className="flex cursor-pointer items-center gap-1.5">
              {!isImmediate && (
                <input
                  type="radio" name="behaviour-mode" className="accent-sky-500"
                  checked={draft.mode === 'periodic'}
                  onChange={() => setDraft((current) => ({ ...current, mode: 'periodic' }))}
                />
              )}
              <span className="text-[11px] text-slate-200">periodic sending</span>
              {!isImmediate && <span className="text-[10px] text-slate-400">every</span>}
              <Input
                type="number" step="any" min={MIN_INTERVAL} value={draft.interval}
                disabled={draft.mode !== 'periodic'}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, interval: event.target.value }))}
                className="!w-20 !py-0.5 !text-[10px]"
              />
              <span className="text-[10px] text-slate-400">s</span>
            </label>
          </div>
          {reactive && draft.mode === 'periodic' && (
            <p className="mt-1 text-[10px] leading-snug text-slate-500">
              Each matching message restarts the schedule with the values it carried —
              one schedule, never two.
            </p>
          )}

          {/* ------------------------------------------------- value copying */}
          {reactive && (
            <>
              <div className="mt-4 flex items-center gap-2">
                <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  Copy from the incoming message
                </h3>
                <Button
                  icon={Plus} className="ml-auto !py-1"
                  disabled={!sourceFields.length || !outFields.length}
                  title={!sourceFields.length || !outFields.length
                    ? 'One of these messages has no field holding a single value'
                    : 'Forward a value from the message that triggered this'}
                  onClick={addMapping}
                >
                  Add
                </Button>
              </div>
              {draft.mappings.length === 0 ? (
                <p className="mt-1.5 rounded-md border border-dashed border-slate-800 px-3 py-2.5 text-[10px] leading-relaxed text-slate-500">
                  The reply sends exactly what is in the compose form. Add a row to carry a
                  value across from the message that triggered it — an id to echo, a state to
                  mirror.
                </p>
              ) : (
                <ul className="mt-1.5 flex flex-col gap-1">
                  {draft.mappings.map((mapping, index) => {
                    const carried = existing?.last_mapped?.[mapping.to];
                    return (
                      <li
                        key={index}
                        className="animate-row-in flex items-center gap-1.5 rounded-md bg-slate-800/60 px-2 py-1.5 ring-1 ring-inset ring-slate-700"
                      >
                        <Select
                          value={mapping.from}
                          onChange={(event) => editMapping(index, { from: event.target.value })}
                          className="!min-w-0 !flex-1 !py-0.5 !text-[10px]"
                        >
                          {sourceFields.map((entry) => (
                            <option key={entry.path} value={entry.path}>{entry.path}</option>
                          ))}
                        </Select>
                        <ArrowRight size={12} className="shrink-0 text-slate-500" />
                        <Select
                          value={mapping.to}
                          onChange={(event) => editMapping(index, { to: event.target.value })}
                          className="!min-w-0 !flex-1 !py-0.5 !text-[10px]"
                        >
                          {outFields.map((entry) => (
                            <option key={entry.path} value={entry.path}>{entry.path}</option>
                          ))}
                        </Select>
                        {/* What actually travelled this row, last time it fired.
                            A mapping reading an absent field is otherwise
                            indistinguishable from a working one. */}
                        <span
                          className="tnum w-14 shrink-0 truncate text-right font-mono text-[10px] text-emerald-500/80"
                          title={carried === undefined
                            ? 'Nothing has come through yet'
                            : `Last value carried: ${carried}`}
                        >
                          {carried === undefined ? '' : String(carried)}
                        </span>
                        <IconButton
                          icon={Trash2} title="Remove this row" variant="danger"
                          className="!h-5 !w-5 shrink-0"
                          onClick={() => removeMapping(index)}
                        />
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          )}

          <p className="mt-4 text-[10px] leading-snug text-slate-500">
            Sends the payload currently in the compose form. Every send is logged in the
            Sent console, exactly like pressing Send Message.
          </p>
          {existing && (
            <p className="mt-1 text-[10px] leading-snug text-slate-500">
              This rule already exists — saving replaces it and resets its counters.
            </p>
          )}

          {error && (
            <div className="mt-3 flex items-start gap-2 rounded-lg border border-rose-900/60 bg-rose-950/30 p-2.5">
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
