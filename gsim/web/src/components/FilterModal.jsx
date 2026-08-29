/**
 * Received filters: which inbound messages are worth logging at all.
 *
 * Shaped like the app, not like a form. The brief describes three stages
 * (connection, then message, then rules) which invites a stepper, but a stepper
 * is wrong for how this is actually used: you keep it open while traffic
 * streams and poke at it repeatedly. So it borrows GSim's own layout instead --
 * a rail that selects, a workspace that configures -- and anyone who knows the
 * main window already knows how to drive it.
 *
 * **Every rule reports what it is actually eating.** That is the one memorable
 * thing here and it is not decoration: `runtime._log` documents why a 60Hz
 * sampler was reverted (an instrument that reports its own sampling period
 * instead of the signal is worse than a slow one), and the only thing that
 * makes deliberate suppression different from that mistake is being counted.
 * So a message row carries its drop count, a rule row carries its own, and the
 * header carries the total -- live, at whatever rate they are moving.
 *
 * Two typefaces do two jobs, which is why a rule reads as a sentence: mono is
 * what exists on the wire (message names, field paths, opcodes, values), sans
 * is the grammar the user wrote (`when`, `is`, `changes`). Set entirely in one
 * face, you cannot tell the field named `Len` from the word "len".
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  AlertCircle, ArrowDownLeft, Eye, Loader2, Plus, Save, Search, Trash2, X,
} from 'lucide-react';
import { api } from '../api';
import { Badge, Button, EmptyState, IconButton, Input, Select, StatusDot, cx } from './ui';
import { fieldTargets, operatorsFor, valueControl } from '../lib/fieldTargets';
import { hexTitle } from '../lib/format';

//: How a message qualifies once the rules have admitted it. Amber throughout
//: for the change modes, because amber already means "held back" in this app
//: (the console's `paused` chip, a behaviour running short of its rate).
const MODES = [
  { value: 'all', label: 'Every time it arrives' },
  { value: 'change', label: 'Only when something changes' },
  { value: 'field-change', label: 'Only when one field changes' },
];

const EMPTY_DRAFT = { mode: 'all', change_field: null, rules: [] };

/** A saved filter, as the draft this dialog edits. */
function toDraft(saved) {
  if (!saved) return { ...EMPTY_DRAFT, rules: [] };
  return {
    mode: saved.mode,
    change_field: saved.change_field,
    rules: saved.rules.map((rule) => ({ ...rule })),
  };
}

function sameDraft(a, b) {
  return JSON.stringify([a.mode, a.change_field ?? null,
    a.rules.map((r) => [r.action, r.path, r.op, r.value])])
    === JSON.stringify([b.mode, b.change_field ?? null,
      b.rules.map((r) => [r.action, r.path, r.op, r.value])]);
}

export default function FilterModal({
  connections, filters, initialConnection,
  onSave, onDelete, onArm, onDisarm, onDisarmAll, onClose,
}) {
  const [connectionName, setConnectionName] = useState(
    initialConnection ?? connections[0]?.name ?? null);
  const [incoming, setIncoming] = useState([]);
  const [loading, setLoading] = useState(false);
  const [unit, setUnit] = useState('');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(null);     // one row of `incoming`
  const [targets, setTargets] = useState([]);
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  // The brief's constraint: with one connection there is nothing to choose, so
  // the control is not rendered at all rather than rendered with one option.
  const oneConnection = connections.length <= 1;
  const connection = connections.find((entry) => entry.name === connectionName) ?? null;
  const forConnection = filters.filter((entry) => entry.connection_name === connectionName);
  const totalDropped = filters.reduce((sum, entry) => sum + entry.dropped, 0);
  const armedCount = filters.filter((entry) => entry.armed).length;

  const savedFor = (row) =>
    row && forConnection.find(
      (entry) => entry.unit_name === row.unit_name && entry.op_code === row.op_code) || null;
  const saved = savedFor(selected);

  /* Which messages this connection can RECEIVE -- from the registry, not from
     what has already arrived. A filter you can only configure after being
     flooded is a filter that arrives too late. */
  useEffect(() => {
    setSelected(null);
    setQuery('');
    setUnit('');
    setError(null);
    if (!connectionName) return setIncoming([]);
    let cancelled = false;
    setLoading(true);
    api.incoming(connectionName).then(
      (rows) => { if (!cancelled) { setIncoming(rows); setLoading(false); } },
      (err) => { if (!cancelled) { setIncoming([]); setError(err.message); setLoading(false); } },
    );
    return () => { cancelled = true; };
  }, [connectionName]);

  /* The selected message's fields. Fetched by the SENDER's unit code and the
     namespace the row carries, so the fields offered are the ones this route
     actually decodes with -- (unitCode, opCode) alone stopped being unique when
     structures became per-link. */
  useEffect(() => {
    if (!selected) { setTargets([]); return; }
    let cancelled = false;
    api.schemaByUnit(selected.unit_code, selected.op_code, selected.namespace).then(
      (schema) => !cancelled && setTargets(fieldTargets(schema)),
      (err) => !cancelled && setError(err.message),
    );
    return () => { cancelled = true; };
  }, [selected]);

  // Selecting a message loads whatever is already configured for it.
  useEffect(() => { setDraft(toDraft(savedFor(selected))); setError(null); }, [selected]);

  const units = useMemo(
    () => [...new Set(incoming.map((row) => row.unit_name))], [incoming]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return incoming.filter((row) => {
      if (unit && row.unit_name !== unit) return false;
      if (!needle) return true;
      // Name, opcode, and the unit that sends it -- the three ways anyone
      // refers to a route, matching how MessagesTable already searches.
      return row.name.toLowerCase().includes(needle)
        || row.op_code_hex.toLowerCase().includes(needle)
        || row.unit_name.toLowerCase().includes(needle);
    });
  }, [incoming, unit, query]);

  const ruleTargets = targets.filter((target) => target.ruleOk);
  const dirty = saved ? !sameDraft(draft, toDraft(saved)) : !sameDraft(draft, EMPTY_DRAFT);

  const run = async (action) => {
    setBusy(true);
    setError(null);
    try { await action(); } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  const apply = () => run(() => onSave(connectionName, {
    unit_name: selected.unit_name,
    op_code: selected.op_code,
    mode: draft.mode,
    change_field: draft.mode === 'field-change' ? draft.change_field : null,
    rules: draft.rules.map(({ action, path, op, value }) => ({ action, path, op, value })),
    armed: saved ? saved.armed : true,
  }));

  const addRule = () => {
    const target = ruleTargets[0];
    if (!target) return;
    setDraft((current) => ({
      ...current,
      rules: [...current.rules, {
        action: 'drop',
        path: target.path,
        op: '==',
        value: target.kind === 'enum' ? (target.options?.[0]?.name ?? null) : 0,
      }],
    }));
  };

  const editRule = (index, patch) => setDraft((current) => ({
    ...current,
    rules: current.rules.map((rule, at) => {
      if (at !== index) return rule;
      const next = { ...rule, ...patch };
      // Retargeting a rule can invalidate its operator and its value, so both
      // are re-seeded from the new field rather than left to fail in the PUT.
      if (patch.path !== undefined) {
        const target = targets.find((entry) => entry.path === patch.path);
        const allowed = operatorsFor(target).map((operator) => operator.value);
        if (!allowed.includes(next.op)) next.op = '==';
        next.value = target?.kind === 'enum' ? (target.options?.[0]?.name ?? null) : 0;
      }
      return next;
    }),
  }));

  const removeRule = (index) => setDraft((current) => ({
    ...current, rules: current.rules.filter((_, at) => at !== index),
  }));

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-6 backdrop-blur-sm"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        onMouseDown={(event) => event.stopPropagation()}
        className="flex h-[min(40rem,90vh)] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-2xl shadow-slate-950/70"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-slate-800 px-4 py-3">
          <ArrowDownLeft size={14} className="text-emerald-400" />
          <h2 className="shrink-0 text-sm font-semibold text-slate-100">Received filters</h2>

          {!oneConnection && (
            <Select
              value={connectionName ?? ''}
              onChange={(event) => setConnectionName(event.target.value)}
              className="!w-auto !py-1 text-[11px]"
            >
              {connections.map((entry) => (
                <option key={entry.name} value={entry.name}>{entry.name}</option>
              ))}
            </Select>
          )}

          <div className="ml-auto flex items-center gap-2">
            {/* The total, always visible: this dialog is allowed to drop
                messages precisely because it never stops saying how many. */}
            {totalDropped > 0 && (
              <span className="tnum font-mono text-[11px] text-slate-500">
                {totalDropped.toLocaleString()} dropped
              </span>
            )}
            <Button
              icon={Eye}
              disabled={busy || armedCount === 0}
              title={armedCount ? `Stop all ${armedCount} filters without deleting them` : 'Nothing is filtering'}
              onClick={() => run(onDisarmAll)}
            >
              Show all
            </Button>
            <IconButton icon={X} title="Close" onClick={onClose} />
          </div>
        </header>

        <div className="flex min-h-0 flex-1">
          {/* ---------------------------------------------------------- rail */}
          <div className="flex w-60 shrink-0 flex-col border-r border-slate-800">
            <div className="shrink-0 space-y-1.5 px-2 py-2">
              {units.length > 1 && (
                <label className="flex items-center gap-1.5">
                  <span className="shrink-0 text-[10px] font-medium uppercase tracking-wider text-slate-500">
                    From
                  </span>
                  <Select
                    value={unit}
                    onChange={(event) => setUnit(event.target.value)}
                    className="!py-1 text-[11px]"
                  >
                    <option value="">All units</option>
                    {units.map((name) => (
                      <option key={name} value={name}>{name}</option>
                    ))}
                  </Select>
                </label>
              )}
              <div className="relative">
                <Search
                  size={12}
                  className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-slate-600"
                />
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Name, unit or opcode…"
                  className="!py-1 pl-7 text-[11px]"
                />
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2">
              {loading ? (
                <EmptyState>Loading messages…</EmptyState>
              ) : visible.length === 0 ? (
                <EmptyState>
                  {incoming.length === 0
                    ? 'This connection has no peers that can send it anything. Add a peer with Structures modules.'
                    : `Nothing matches “${query}”.`}
                </EmptyState>
              ) : (
                <ul className="flex flex-col gap-0.5">
                  {visible.map((row) => {
                    const rowFilter = savedFor(row);
                    const isActive = selected?.op_code === row.op_code
                      && selected?.unit_name === row.unit_name;
                    return (
                      <li key={`${row.unit_name}-${row.op_code}`}>
                        <button
                          type="button"
                          onClick={() => setSelected(row)}
                          className={cx(
                            'flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left',
                            'transition-colors duration-100',
                            'focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/70',
                            isActive ? 'bg-sky-500/10 ring-1 ring-sky-500/30' : 'hover:bg-slate-800/60',
                          )}
                        >
                          {/* Armed vs merely configured, told the same way the
                              rest of the app tells running vs armed. */}
                          <StatusDot
                            on={!!rowFilter?.armed}
                            className={cx('h-1.5 w-1.5', !rowFilter && 'opacity-0')}
                          />
                          <div className="min-w-0 flex-1">
                            <div className={cx('truncate font-mono text-[11px]',
                              isActive ? 'text-sky-300' : 'text-slate-200')}>
                              {row.name}
                            </div>
                            <div className="truncate font-mono text-[9px] text-slate-500">
                              {row.unit_name} · {row.op_code_hex}
                            </div>
                          </div>
                          {rowFilter?.dropped > 0 && (
                            <span
                              className="tnum shrink-0 font-mono text-[10px] text-amber-500/90"
                              title={`${rowFilter.dropped} dropped, ${rowFilter.logged} logged`}
                            >
                              {rowFilter.dropped.toLocaleString()}
                            </span>
                          )}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </div>

          {/* ----------------------------------------------------- workspace */}
          {!selected ? (
            <div className="flex min-h-0 flex-1 flex-col">
              <EmptyState icon={ArrowDownLeft}>
                Pick a message to say when it should be logged. Everything else keeps
                arriving exactly as it does now.
              </EmptyState>
            </div>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col">
              <div className="min-h-0 flex-1 overflow-y-auto p-4">
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-sm font-semibold text-slate-100">
                    {selected.name}
                  </span>
                  <Badge tone="emerald">
                    <span title={hexTitle(selected.op_code, 'opCode')}>{selected.op_code_hex}</span>
                  </Badge>
                  {saved && (
                    <span className="tnum ml-auto font-mono text-[10px] text-slate-500">
                      {saved.dropped.toLocaleString()} dropped ·{' '}
                      {saved.logged.toLocaleString()} logged
                    </span>
                  )}
                </div>
                <p className="mt-0.5 font-mono text-[10px] text-slate-500">
                  from {selected.unit_name} · unit {`0x${selected.unit_code.toString(16).toUpperCase().padStart(2, '0')}`}
                  {' · '}{selected.field_count} field{selected.field_count === 1 ? '' : 's'}
                </p>

                {/* ------------------------------------------- log this when */}
                <h3 className="mt-4 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  Log this message
                </h3>
                <div className="mt-1.5 flex flex-col gap-0.5" role="radiogroup" aria-label="Log this message">
                  {MODES.map((mode) => {
                    const on = draft.mode === mode.value;
                    return (
                      <label
                        key={mode.value}
                        className={cx(
                          'flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5',
                          'transition-colors duration-100',
                          on ? 'bg-amber-500/10 ring-1 ring-inset ring-amber-500/25' : 'hover:bg-slate-800/50',
                          on && mode.value === 'all' && '!bg-slate-800 !ring-slate-700',
                        )}
                      >
                        <input
                          type="radio"
                          name="filter-mode"
                          className="accent-amber-500"
                          checked={on}
                          onChange={() => setDraft((current) => ({
                            ...current,
                            mode: mode.value,
                            change_field: mode.value === 'field-change'
                              ? (current.change_field ?? targets[0]?.path ?? null)
                              : null,
                          }))}
                        />
                        <span className={cx('text-[11px]', on ? 'text-slate-100' : 'text-slate-400')}>
                          {mode.label}
                        </span>
                        {mode.value === 'field-change' && draft.mode === 'field-change' && (
                          <Select
                            value={draft.change_field ?? ''}
                            onChange={(event) =>
                              setDraft((current) => ({ ...current, change_field: event.target.value }))}
                            onClick={(event) => event.preventDefault()}
                            className="!w-auto !py-0.5 !text-[10px]"
                          >
                            {targets.map((target) => (
                              <option key={target.path} value={target.path}>{target.path}</option>
                            ))}
                          </Select>
                        )}
                        {on && mode.value !== 'all' && saved?.dropped_by_change > 0 && (
                          <span className="tnum ml-auto font-mono text-[10px] text-amber-500/90">
                            {saved.dropped_by_change.toLocaleString()} held back
                          </span>
                        )}
                      </label>
                    );
                  })}
                </div>
                {draft.mode === 'change' && (
                  <p className="mt-1 pl-2 text-[10px] leading-snug text-slate-500">
                    Compares the whole message, arrays included.
                  </p>
                )}

                {/* -------------------------------------------------- rules */}
                <div className="mt-4 flex items-center gap-2">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                    Rules
                  </h3>
                  <Button
                    icon={Plus}
                    className="ml-auto !py-1"
                    disabled={ruleTargets.length === 0}
                    title={ruleTargets.length === 0
                      ? 'No field in this message holds a single value to compare'
                      : 'Add a keep or drop rule'}
                    onClick={addRule}
                  >
                    Add rule
                  </Button>
                </div>

                {draft.rules.length === 0 ? (
                  <p className="mt-1.5 rounded-md border border-dashed border-slate-800 px-3 py-3 text-[10px] leading-relaxed text-slate-500">
                    {ruleTargets.length === 0
                      ? `Every field of ${selected.name} is a struct or a repeating array, so there is nothing a rule can compare. Watch it for changes instead.`
                      : 'No rules — every arrival qualifies. Add one to keep or drop messages by what a field holds.'}
                  </p>
                ) : (
                  <ul className="mt-1.5 flex flex-col gap-1">
                    {draft.rules.map((rule, index) => {
                      const target = targets.find((entry) => entry.path === rule.path);
                      const control = valueControl(target);
                      const keep = rule.action === 'keep';
                      const savedRule = saved?.rules?.[index];
                      return (
                        <li
                          key={index}
                          className={cx(
                            'animate-row-in flex items-center gap-1.5 rounded-md px-2 py-1.5',
                            'ring-1 ring-inset transition-colors duration-100',
                            // Keep is emerald (this app's "live inbound"); drop
                            // is grey, because grey is what dropping does. Rose
                            // is reserved for errors, and a drop is a choice.
                            keep ? 'bg-emerald-500/10 ring-emerald-500/25'
                              : 'bg-slate-800/60 ring-slate-700',
                          )}
                        >
                          <Select
                            value={rule.action}
                            onChange={(event) => editRule(index, { action: event.target.value })}
                            className={cx('!w-[4.5rem] !py-0.5 !text-[10px] !font-semibold',
                              keep ? '!text-emerald-300' : '!text-slate-300')}
                          >
                            <option value="drop">Drop</option>
                            <option value="keep">Keep</option>
                          </Select>
                          <span className="shrink-0 text-[10px] text-slate-500">when</span>
                          <Select
                            value={rule.path}
                            onChange={(event) => editRule(index, { path: event.target.value })}
                            className="!w-auto !min-w-0 !flex-1 !py-0.5 !text-[10px]"
                          >
                            {ruleTargets.map((entry) => (
                              <option key={entry.path} value={entry.path}>{entry.path}</option>
                            ))}
                          </Select>
                          <Select
                            value={rule.op}
                            onChange={(event) => editRule(index, { op: event.target.value })}
                            className="!w-[4.5rem] !py-0.5 !text-[10px]"
                          >
                            {operatorsFor(target).map((operator) => (
                              <option key={operator.value} value={operator.value}>
                                {operator.label}
                              </option>
                            ))}
                          </Select>
                          {control.type === 'enum' ? (
                            <Select
                              value={rule.value ?? ''}
                              onChange={(event) => editRule(index, { value: event.target.value })}
                              className="!w-28 !py-0.5 !text-[10px]"
                            >
                              {control.options.map((option) => (
                                <option key={option.name} value={option.name}>{option.name}</option>
                              ))}
                            </Select>
                          ) : (
                            <Input
                              type="number"
                              value={rule.value ?? ''}
                              min={control.min}
                              max={control.max}
                              onChange={(event) => editRule(index, {
                                value: event.target.value === '' ? '' : Number(event.target.value),
                              })}
                              className="!w-20 !py-0.5 !text-[10px]"
                            />
                          )}
                          {/* What this rule is actually doing. The number is
                              the whole argument for letting a filter drop
                              anything at all. */}
                          <span
                            className={cx('tnum w-12 shrink-0 text-right font-mono text-[10px]',
                              keep ? 'text-emerald-500/80' : 'text-amber-500/90')}
                            title={savedRule
                              ? `${savedRule.hits} messages ${keep ? 'kept' : 'dropped'} by this rule`
                              : 'Not applied yet'}
                          >
                            {savedRule?.hits ? savedRule.hits.toLocaleString() : ''}
                          </span>
                          <IconButton
                            icon={Trash2} title="Remove this rule" variant="danger"
                            className="!h-5 !w-5 shrink-0"
                            onClick={() => removeRule(index)}
                          />
                        </li>
                      );
                    })}
                  </ul>
                )}

                {/* The combination rule, stated where it applies. Two keeps and
                    a drop is otherwise a guess. */}
                {draft.rules.length > 1 && (
                  <p className="mt-2 text-[10px] leading-snug text-slate-500">
                    Drop wins. If any Keep rule exists, a message must match one to be logged.
                  </p>
                )}

                {error && (
                  <div className="mt-3 flex items-start gap-2 rounded-lg border border-rose-900/60 bg-rose-950/30 p-2.5">
                    <AlertCircle size={14} className="mt-px shrink-0 text-rose-400" />
                    <pre className="min-w-0 whitespace-pre-wrap font-mono text-[11px] text-rose-300">
                      {error}
                    </pre>
                  </div>
                )}
              </div>

              <footer className="flex shrink-0 items-center gap-2 border-t border-slate-800 px-4 py-3">
                {saved && (
                  <>
                    <IconButton
                      icon={Trash2} title="Remove this filter" variant="danger"
                      disabled={busy} onClick={() => run(() => onDelete(saved.id))}
                    />
                    <Button
                      disabled={busy}
                      onClick={() => run(() => (saved.armed ? onDisarm(saved.id) : onArm(saved.id)))}
                      title={saved.armed
                        ? 'Stop filtering this message, keeping the rules'
                        : 'Start filtering again, from a clean count'}
                    >
                      {saved.armed ? 'Pause' : 'Resume'}
                    </Button>
                  </>
                )}
                {/* Dropping happens on the server, so an unapplied draft is not
                    doing anything yet -- said plainly rather than implied by a
                    button state nobody reads. */}
                {dirty && (
                  <span className="text-[10px] text-amber-400">Not applied yet</span>
                )}
                <Button
                  variant="primary"
                  className={cx('ml-auto', busy && '[&_svg]:animate-spin')}
                  icon={busy ? Loader2 : Save}
                  disabled={busy || !dirty}
                  onClick={apply}
                >
                  {saved ? 'Save changes' : 'Apply filter'}
                </Button>
              </footer>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
