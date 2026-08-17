/**
 * Inspector -- one panel, two modes.
 *
 *   compose  a message picked in the workspace  -> editable card form
 *   inspect  a log line picked in the console   -> read-only parsed view
 *
 * Both render through the same recursive `FieldRenderer`, so an inbound message
 * displays with exactly the structure its form would have had.
 */
import React, { useEffect, useState } from 'react';
import {
  AlertCircle, ArrowDownLeft, ArrowUpRight, FileJson, Loader2, MousePointerClick,
  Repeat, RotateCcw, Send,
} from 'lucide-react';
import FieldRenderer, { FieldList } from './FieldRenderer';
import { Badge, Button, EmptyState, Panel, PanelHeader, Select, cx } from './ui';
import { defaultPayload, payloadSize } from '../lib/schema';
import { api } from '../api';

const hex4 = (value) => `0x${Number(value).toString(16).toUpperCase().padStart(4, '0')}`;
const hex2 = (value) => `0x${Number(value).toString(16).toUpperCase().padStart(2, '0')}`;

/**
 * Message header block -- top-left of the panel in both modes.
 *
 * Puts the routing facts (who it goes to / came from) next to the wire facts
 * (unitCode, opCode, length) in one place, so the thing that identifies a
 * message on the wire is visible while you build or read it. `length` is the
 * payload size only -- the 5-byte framing header core prepends is separate and
 * not something the user controls.
 */
function HeaderBlock({ title, doc, unitCode, opCode, length, children }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-slate-100">{title}</h3>
          {doc && <p className="mt-0.5 text-[11px] leading-snug text-slate-500">{doc}</p>}
        </div>
      </div>

      <dl className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-slate-800 pt-2.5">
        <HeaderStat label="unitCode" value={`${hex2(unitCode)} · ${unitCode}`} />
        <HeaderStat label="opCode" value={`${hex4(opCode)} · ${opCode}`} />
        <HeaderStat label="length" value={`${length} B`} />
      </dl>

      {children && <div className="mt-2.5 border-t border-slate-800 pt-2.5">{children}</div>}
    </div>
  );
}

function HeaderStat({ label, value }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="text-[10px] font-medium uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="font-mono text-[11px] text-slate-200">{value}</dd>
    </div>
  );
}

export default function Inspector({
  connectionName, selection, peers, destination, onSent, onBehaviour, behaviour,
  draftKey, drafts,
}) {
  if (!selection) {
    return (
      <Panel className="min-w-0 flex-1">
        <PanelHeader title="Inspector" icon={FileJson} />
        <EmptyState icon={MousePointerClick}>
          Pick a message above to compose one, or a console entry to inspect it.
        </EmptyState>
      </Panel>
    );
  }
  return selection.mode === 'compose' ? (
    <ComposeForm
      // Destination is part of the identity: it selects the layout, so
      // switching peers must rebuild the form, not reuse the old schema.
      key={`${connectionName}:${destination}:${selection.opCode}`}
      connectionName={connectionName}
      opCode={selection.opCode}
      peers={peers}
      destination={destination}
      onSent={onSent}
      onBehaviour={onBehaviour}
      behaviour={behaviour}
      draftKey={draftKey}
      drafts={drafts}
    />
  ) : (
    <LogDetails key={selection.entry.seq} entry={selection.entry} />
  );
}

/* ------------------------------------------------------------------ */
/* Compose                                                             */
/* ------------------------------------------------------------------ */
function ComposeForm({
  connectionName, opCode, peers, destination, onSent, onBehaviour, behaviour,
  draftKey, drafts,
}) {
  const [schema, setSchema] = useState(null);
  const [payload, setPayloadState] = useState({});
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState(false);

  // The destination is chosen in the Messages panel, because it selects which
  // messages exist at all -- see MessagesTable's docstring.
  const unitName = destination ?? '';
  const peer = peers.find((entry) => entry.name === unitName) ?? null;

  // Every payload change is mirrored into the caller's draft store, which
  // outlives this component. This form is remounted whenever the route changes
  // (the schema differs per route, so reusing the old state would render the
  // wrong fields), and clicking a log entry unmounts it entirely -- without an
  // external store, a half-filled message was lost on any of those.
  const setPayload = (next) => {
    setPayloadState((current) => {
      const resolved = typeof next === 'function' ? next(current) : next;
      if (drafts && draftKey) drafts.set(draftKey, resolved);
      return resolved;
    });
  };

  useEffect(() => {
    let cancelled = false;
    setSchema(null);
    setError(null);
    api.messageSchema(connectionName, opCode, unitName).then(
      (next) => {
        if (cancelled) return;
        setSchema(next);
        // Restore an in-progress draft for this exact route if there is one;
        // otherwise start fully populated, so every input is controlled from
        // first render and the zeros that will be sent are visible up front.
        const saved = drafts && draftKey ? drafts.get(draftKey) : undefined;
        setPayloadState(saved ?? defaultPayload(next));
      },
      (err) => !cancelled && setError(err.message),
    );
    return () => { cancelled = true; };
  }, [connectionName, opCode, unitName, draftKey]);

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const entry = await api.send(connectionName, { unit_name: unitName, op_code: opCode, payload });
      onSent?.(entry);
      setFlash(true);
      setTimeout(() => setFlash(false), 1200);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (!schema) {
    return (
      <Panel className="min-w-0 flex-1">
        <PanelHeader title="Inspector" icon={FileJson} />
        {error ? (
          <EmptyState icon={AlertCircle}>{error}</EmptyState>
        ) : (
          <div className="flex flex-1 items-center justify-center gap-2 text-xs text-slate-500">
            <Loader2 size={14} className="animate-spin" />
            Loading schema…
          </div>
        )}
      </Panel>
    );
  }

  return (
    <Panel className="min-w-0 flex-1">
      <PanelHeader title="Compose" icon={FileJson}>
        <Badge tone="sky">{`0x${opCode.toString(16).toUpperCase().padStart(4, '0')}`}</Badge>
      </PanelHeader>

      <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <div className="mx-auto flex max-w-3xl flex-col gap-3">
            <HeaderBlock
              title={schema.name}
              doc={schema.doc}
              unitCode={schema.unit_code}
              opCode={opCode}
              length={payloadSize(schema.fields, payload)}
            >
              {/* Read-only: the destination is picked in the Messages panel,
                  where it also decides which messages are listed. */}
              <div className="flex items-baseline gap-1.5">
                <span className="text-[10px] font-medium uppercase tracking-wider text-slate-500">
                  Destination
                </span>
                <span className="font-mono text-[11px] text-slate-200">
                  {peer ? `${peer.name} · unitCode ${peer.unit_code}` : unitName || '—'}
                </span>
              </div>
              {schema.namespace && (
                <div className="flex items-baseline gap-1.5">
                  <span className="text-[10px] font-medium uppercase tracking-wider text-slate-500">
                    IRS
                  </span>
                  <span
                    className="truncate font-mono text-[11px] text-slate-400"
                    title={schema.namespace}
                  >
                    {schema.namespace.split('.').slice(-2).join('.')}
                  </span>
                </div>
              )}
            </HeaderBlock>

            <div className="flex flex-col gap-2.5 rounded-lg border border-slate-800 bg-slate-900/60 p-3">
              <FieldList fields={schema.fields} value={payload} onChange={setPayload} />
            </div>

            {error && (
              <div className="flex items-start gap-2 rounded-lg border border-rose-900/60 bg-rose-950/30 p-2.5">
                <AlertCircle size={14} className="mt-px shrink-0 text-rose-400" />
                <pre className="min-w-0 whitespace-pre-wrap font-mono text-[11px] text-rose-300">
                  {error}
                </pre>
              </div>
            )}
          </div>
        </div>

        <footer className="flex shrink-0 items-center gap-2 border-t border-slate-800 bg-slate-900 px-3 py-2">
          {/* Left of Send: sending automatically is a variation on sending,
              and it carries the payload currently in this form. */}
          <Button
            icon={Repeat}
            disabled={!unitName}
            onClick={() => onBehaviour?.({ opCode, payload, messageName: schema.name })}
            className={cx(behaviour?.active && '!border-emerald-700 !text-emerald-300')}
          >
            Behaviour
            {behaviour && (
              <span className="ml-0.5 font-mono text-[10px] opacity-70">
                {behaviour.interval}s
              </span>
            )}
          </Button>
          <Button
            type="submit"
            variant="primary"
            icon={busy ? Loader2 : Send}
            disabled={busy || !unitName}
            className={cx(busy && '[&_svg]:animate-spin')}
          >
            {busy ? 'Sending…' : 'Send Message'}
          </Button>
          <Button icon={RotateCcw} onClick={() => setPayload(defaultPayload(schema))}>
            Reset
          </Button>
          {flash && (
            <span className="ml-1 text-[11px] font-medium text-emerald-400">Sent ✓</span>
          )}
          <span className="ml-auto font-mono text-[10px] text-slate-600">
            empty fields send as 0
          </span>
        </footer>
      </form>
    </Panel>
  );
}

/* ------------------------------------------------------------------ */
/* Inspect                                                             */
/* ------------------------------------------------------------------ */
function LogDetails({ entry }) {
  const [schema, setSchema] = useState(null);
  const isSent = entry.direction === 'sent';

  useEffect(() => {
    let cancelled = false;
    setSchema(null);
    // A received message decodes with the SENDER's unit code, so the entry
    // carries the code its layout is registered under -- ours on send, the
    // peer's on receive. The wrong one finds the wrong layout, or none.
    // `namespace` pins the exact layout this entry was decoded with --
    // (unit_code, op_code) alone stopped being unique once structures became
    // per-link, so without it an inspected message could render against the
    // other link's fields.
    api.schemaByUnit(entry.unit_code, entry.op_code, entry.namespace).then(
      (next) => !cancelled && setSchema(next),
      () => {},
    );
    return () => { cancelled = true; };
  }, [entry.unit_code, entry.op_code]);

  return (
    <Panel className="min-w-0 flex-1">
      <PanelHeader title="Inspector" icon={FileJson}>
        <Badge tone={isSent ? 'sky' : 'emerald'}>
          {isSent ? <ArrowUpRight size={10} /> : <ArrowDownLeft size={10} />}
          {isSent ? 'sent' : 'received'}
        </Badge>
      </PanelHeader>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <div className="mx-auto flex max-w-3xl flex-col gap-3">
          <HeaderBlock
            title={entry.message_name}
            doc={schema?.doc}
            unitCode={entry.unit_code}
            opCode={entry.op_code}
            length={schema ? payloadSize(schema.fields, entry.payload) : 0}
          >
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
              <div className="flex items-baseline gap-1.5">
                <span className="text-[10px] font-medium uppercase tracking-wider text-slate-500">
                  {isSent ? 'Destination' : 'Source'}
                </span>
                <span className="font-mono text-[11px] text-slate-200">{entry.unit_name}</span>
              </div>
              <div className="flex items-baseline gap-1.5">
                <span className="text-[10px] font-medium uppercase tracking-wider text-slate-500">
                  Connection
                </span>
                <span className="font-mono text-[11px] text-slate-300">
                  {entry.connection_name}
                </span>
              </div>
              <span className="font-mono text-[10px] text-slate-600">
                {new Date(entry.timestamp * 1000).toLocaleTimeString()}
              </span>
            </div>
          </HeaderBlock>

          {entry.error && (
            <div className="flex items-start gap-2 rounded-lg border border-rose-900/60 bg-rose-950/30 p-2.5">
              <AlertCircle size={14} className="mt-px shrink-0 text-rose-400" />
              <span className="font-mono text-[11px] text-rose-300">{entry.error}</span>
            </div>
          )}

          {schema && entry.payload ? (
            <div className="flex flex-col gap-2.5 rounded-lg border border-slate-800 bg-slate-900/60 p-3">
              <FieldList fields={schema.fields} value={entry.payload} onChange={() => {}} readOnly />
            </div>
          ) : (
            <pre className="overflow-x-auto rounded-lg border border-slate-800 bg-slate-950/60 p-3 font-mono text-[11px] text-slate-300">
              {JSON.stringify(entry.payload, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </Panel>
  );
}
