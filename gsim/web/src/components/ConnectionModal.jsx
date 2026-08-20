/**
 * Create / Edit connection.
 *
 * Enforces GSim's stricter contract client-side for immediate feedback (at
 * least one peer, at least one Structures module), but the server is the real
 * gate (`gsim/api/models.py`) and its errors are surfaced verbatim -- a config
 * core rejects should read as "connections 'A' and 'B' both use unitCode 7",
 * not "request failed".
 */
import React, { useEffect, useRef, useState } from 'react';
import { AlertCircle, FolderOpen, Loader2, Plus, Save, Trash2, X } from 'lucide-react';
import { Badge, Button, Field, IconButton, Input, PathInput, Select, cx, sideLabel } from './ui';
import FilePickerModal from './FilePickerModal';

//: Only present inside the pywebview desktop shell -- `--server`/browser mode
//: has no native file dialog, so the Browse button hides rather than errors.
const canBrowseFiles = () => typeof window !== 'undefined' && !!window.pywebview?.api?.browse_structures_file;

// Server-ish side first in each list: it is the default a new connection gets,
// so the listening half of a link is what you build without extra clicks.
const SIDES = {
  tcp: ['server', 'client'],
  udp: ['server', 'client'],
  multicast: ['receiver', 'sender'],
  dds: ['subscriber', 'publisher'],
};

// Echo is a point-to-point heartbeat; core arms it per connected peer. It has
// no meaning for the fan-out protocols, so the section is hidden for them
// rather than offered and ignored.
const ECHO_PROTOCOLS = new Set(['tcp', 'udp']);

// A structures file defines the IRS for ONE link, so it belongs to a peer --
// even when there is only one. Multicast is the sole exception core makes:
// one sender fans out to many receivers over a single shared IRS, so a
// connection-level list stays legal there (and only there).
const FANS_OUT = new Set(['multicast']);
const sharedStructures = (form) => FANS_OUT.has(form.protocol);

// core never reads "ip" (the remote address) for a listening endpoint -- a
// TCP/UDP server binds only `local_ip` and learns its peer from whoever
// connects; DDS is data-centric and doesn't use either IP at all. Offering
// the field there is offering a value nothing will ever use. `ip` still goes
// out in the submitted config either way (core requires the key); it is just
// pinned to a harmless default instead of asked of the user.
const remoteIpVisible = (form) => {
  if (form.protocol === 'dds') return false;
  if (form.protocol === 'multicast') return true;   // the group address, needed by both sides
  return form.side === 'client';
};
const remoteIpLabel = (form) => (form.protocol === 'multicast' ? 'Multicast IP' : 'Remote IP');
const HIDDEN_IP_DEFAULT = '0.0.0.0';

const BLANK_PEER = {
  name: '', port: '', unitCode: '', structures: [''],
  // Per-peer echo override -- blank means "inherit the connection's default".
  echo_opcode: '', echo_interval: '', echo_timeout: '',
};

const BLANK = {
  name: '', protocol: 'tcp', side: 'server', ip: '127.0.0.1', local_ip: '127.0.0.1',
  // No default port or unitCode: a silently-accepted 0 is worse than an empty
  // required field, because 0 is a legal value the user never chose.
  unitCode: '', peers: [{ ...BLANK_PEER }], structures: [''],
  echo_opcode: '', echo_interval: '', echo_timeout: '',
};

/**
 * Parse a unit/op code written as decimal ("31") or 0x-prefixed hex ("0x1F").
 * Returns null when it is not a number at all, so the caller can reject it
 * rather than silently sending NaN.
 *
 * Bare hex ("1f") is deliberately NOT accepted: without the prefix there is no
 * way to tell "10" meaning ten from "10" meaning sixteen, and silently picking
 * one would corrupt a unit code.
 */
function parseCode(raw) {
  const text = String(raw ?? '').trim();
  if (text === '') return null;
  if (/^0[xX][0-9a-fA-F]+$/.test(text)) return parseInt(text.slice(2), 16);
  if (/^\d+$/.test(text)) return parseInt(text, 10);
  return null;
}

/**
 * Input mask for code fields: only characters that can appear in a decimal or
 * 0x-hex literal survive keystroke-by-keystroke, so an unparseable value can
 * never be typed in the first place.
 *
 * `x` is allowed only as the second character of a leading `0`, and the
 * hex letters a-f only once that prefix exists.
 */
function maskCode(raw) {
  const text = String(raw ?? '').replace(/\s+/g, '');
  if (/^0[xX]/.test(text)) {
    return `0x${text.slice(2).replace(/[^0-9a-fA-F]/g, '').toUpperCase()}`;
  }
  // Permit the moment mid-typing where the user has just entered "0" and is
  // about to type "x"; everything else collapses to digits.
  const digits = text.replace(/[^0-9xX]/g, '');
  if (/^0[xX]$/.test(digits)) return '0x';
  return digits.replace(/[^0-9]/g, '');
}

const codeInvalid = (raw, max) => {
  const value = parseCode(raw);
  return value === null || value < 0 || value > max;
};

/**
 * IPv4 input mask: digits only, auto-dotted after each 3-digit octet, capped
 * at four octets of <= 255.
 *
 * Typed dots are honoured too, so "10.0.0.1" (octets shorter than 3 digits)
 * still works -- auto-dotting alone could never produce it, since it only
 * fires when an octet fills up.
 */
function maskIp(raw, previous) {
  // Backspacing over an auto-inserted dot must stick, not be re-added.
  if (previous && previous.endsWith('.') && raw === previous.slice(0, -1)) return raw;

  const octets = [];
  let current = '';
  let openSeparator = false;   // last thing consumed ended an octet

  for (const char of raw) {
    if (octets.length === 4) break;
    if (char === '.') {
      if (current !== '') { octets.push(current); current = ''; openSeparator = true; }
      continue;
    }
    if (!/\d/.test(char)) continue;
    current += char;
    openSeparator = false;
    if (current.length === 3) { octets.push(current); current = ''; openSeparator = true; }
  }

  const parts = octets.map((octet) => String(Math.min(255, parseInt(octet, 10))));
  if (current !== '') parts.push(current);

  // Emit the implied dot so the caret lands in the next octet on its own.
  return parts.join('.') + (openSeparator && parts.length < 4 ? '.' : '');
}

export default function ConnectionModal({ initial, onSubmit, onClose }) {
  const [form, setForm] = useState(() => ({ ...BLANK, ...(initial ?? {}) }));
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  // pywebview injects `window.pywebview` asynchronously (its own
  // `pywebviewready` event, after our own mount), so a plain one-shot check
  // at render time would miss it if the modal opens early.
  const [canBrowse, setCanBrowse] = useState(canBrowseFiles);
  // Where the current click STARTED. A click event fires on the nearest common
  // ancestor of mousedown and mouseup, so selecting text inside the dialog and
  // releasing over the backdrop would otherwise register as a backdrop click
  // and discard the form mid-edit.
  const pressStartedOnBackdrop = useRef(false);

  const showEcho = ECHO_PROTOCOLS.has(form.protocol);
  const showSharedStructures = sharedStructures(form);

  useEffect(() => {
    const onKey = (event) => event.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    if (canBrowse) return;
    const onReady = () => setCanBrowse(canBrowseFiles());
    window.addEventListener('pywebviewready', onReady);
    return () => window.removeEventListener('pywebviewready', onReady);
  }, [canBrowse]);

  /** Where a picked path should be written back. `peerIndex === null` targets
   *  the connection-level list. Held in state while the in-app picker is open,
   *  since that modal reports its result asynchronously. */
  const [picking, setPicking] = useState(null);

  const applyStructurePath = (path, { index, peerIndex }) => {
    if (peerIndex === null) {
      set('structures', form.structures.map((s, i) => (i === index ? path : s)));
    } else {
      setPeerStructures(peerIndex, (list) => list.map((s, i) => (i === index ? path : s)));
    }
  };

  /** Native dialog in the desktop shell, in-app picker in a browser tab. The
   *  button is offered EITHER WAY -- previously it only appeared under
   *  pywebview, so `--server` mode had no way to browse at all. */
  const browseForStructureFile = async (index, peerIndex = null) => {
    if (!canBrowse) return setPicking({ index, peerIndex });
    try {
      const path = await window.pywebview.api.browse_structures_file();
      if (path) applyStructurePath(path, { index, peerIndex });
    } catch (err) {
      setError(err.message ?? String(err));
    }
  };

  const setPeerStructures = (peerIndex, update) =>
    setForm((f) => ({
      ...f,
      peers: f.peers.map((peer, i) =>
        i === peerIndex ? { ...peer, structures: update(peer.structures ?? ['']) } : peer,
      ),
    }));

  const set = (key, value) => setForm((f) => ({ ...f, [key]: value }));
  const setPeer = (index, key, value) =>
    setForm((f) => ({
      ...f,
      peers: f.peers.map((peer, i) => (i === index ? { ...peer, [key]: value } : peer)),
    }));

  const submit = async (event) => {
    event.preventDefault();

    // Codes accept hex, so they are text inputs -- `required` alone cannot
    // catch "0xZZ". Validate before we let anything reach the API.
    if (codeInvalid(form.unitCode, 0xff)) {
      return setError("Our unitCode must be 0-255 (decimal, or hex like 0x1F).");
    }
    const badPeer = form.peers.find((peer) => codeInvalid(peer.unitCode, 0xff));
    if (badPeer) {
      return setError(`Connected unit "${badPeer.name || '—'}": unitCode must be 0-255 (or hex like 0x1F).`);
    }
    if (showEcho && form.echo_opcode !== '' && codeInvalid(form.echo_opcode, 0xffff)) {
      return setError('Echo opCode must be 0-65535 (decimal, or hex like 0x003C).');
    }
    if (showEcho) {
      const badEchoPeer = form.peers.find(
        (peer) => peer.echo_opcode !== '' && codeInvalid(peer.echo_opcode, 0xffff),
      );
      if (badEchoPeer) {
        return setError(
          `Connected unit "${badEchoPeer.name || '—'}": echo opCode must be 0-65535 (decimal, or hex like 0x003C).`,
        );
      }
      const mistimedPeer = form.peers.find(
        (peer) =>
          peer.echo_interval !== '' && peer.echo_timeout !== '' &&
          Number(peer.echo_timeout) <= Number(peer.echo_interval),
      );
      if (mistimedPeer) {
        return setError(`Connected unit "${mistimedPeer.name || '—'}": echo timeout must exceed interval.`);
      }
    }

    // Mirror the server's rule so the reason is visible before a round trip.
    const clean = (list) => (list ?? []).filter((s) => s.trim());
    if (!showSharedStructures) {
      const bare = form.peers.filter((peer) => clean(peer.structures).length === 0);
      if (bare.length) {
        return setError(
          `Connected unit${bare.length > 1 ? 's' : ''} ` +
          `${bare.map((p) => `"${p.name || '—'}"`).join(', ')}: ` +
          'each unit needs its own IRS structures. A structures file defines the messages ' +
          'for one link, so with several connected units there is no shared list to fall back on.',
        );
      }
    }

    setBusy(true);
    setError(null);
    try {
      await onSubmit({
        ...form,
        // Pinned rather than asked for once the field is hidden -- core still
        // requires the key, it just never reads it for a listening endpoint.
        ip: remoteIpVisible(form) ? form.ip : HIDDEN_IP_DEFAULT,
        unitCode: parseCode(form.unitCode),
        peers: form.peers.map((peer) => ({
          ...peer,
          port: Number(peer.port),
          unitCode: parseCode(peer.unitCode),
          structures: showSharedStructures ? null : clean(peer.structures),
          // Same reasoning as the connection-level echo keys below: never
          // submit a value for a protocol/peer that can no longer see it.
          echo_opcode: !showEcho || peer.echo_opcode === '' ? null : parseCode(peer.echo_opcode),
          echo_interval: !showEcho || peer.echo_interval === '' ? null : Number(peer.echo_interval),
          echo_timeout: !showEcho || peer.echo_timeout === '' ? null : Number(peer.echo_timeout),
        })),
        // Never submit a connection-level list when it is not legal -- a value
        // left behind by adding a peer would be rejected by core, same
        // defensive strip the echo keys get below.
        structures: showSharedStructures ? clean(form.structures) : [],
        // Never submit echo keys for a protocol that does not offer them --
        // otherwise a value left behind by switching protocol would arm a
        // heartbeat the user can no longer see or edit.
        echo_opcode: !showEcho || form.echo_opcode === '' ? null : parseCode(form.echo_opcode),
        echo_interval: !showEcho || form.echo_interval === '' ? null : Number(form.echo_interval),
        echo_timeout: !showEcho || form.echo_timeout === '' ? null : Number(form.echo_timeout),
      });
      onClose();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-6 backdrop-blur-sm"
      onMouseDown={(event) => {
        pressStartedOnBackdrop.current = event.target === event.currentTarget;
      }}
      onClick={(event) => {
        // Close only when the press both STARTED and ENDED on the backdrop.
        if (event.target === event.currentTarget && pressStartedOnBackdrop.current) onClose();
      }}
    >
      <div
        onMouseDown={(event) => event.stopPropagation()}
        className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-2xl shadow-slate-950/70"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-slate-800 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-100">
            {initial ? 'Edit Connection' : 'New Connection'}
          </h2>
          <IconButton icon={X} title="Close" onClick={onClose} className="ml-auto" />
        </header>

        <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
            <Section title="Identity">
              <Field label="Name" htmlFor="c-name">
                <Input
                  id="c-name" value={form.name} required
                  placeholder="Insert simulated unit name"
                  onChange={(e) => set('name', e.target.value)} className="!font-sans"
                />
              </Field>
              <div className="flex gap-3">
                <Field label="Protocol">
                  <Select
                    value={form.protocol}
                    onChange={(e) => {
                      set('protocol', e.target.value);
                      set('side', SIDES[e.target.value][0]);
                    }}
                  >
                    {Object.keys(SIDES).map((p) => <option key={p} value={p}>{p}</option>)}
                  </Select>
                </Field>
                <Field label="Side">
                  <Select value={form.side} onChange={(e) => set('side', e.target.value)}>
                    {SIDES[form.protocol].map((s) => (
                      <option key={s} value={s}>{sideLabel(form.protocol, s)}</option>
                    ))}
                  </Select>
                </Field>
                {/* Text, not number, so "0x1F" can be typed. */}
                <Field label="Our unitCode" hint="decimal or 0x hex">
                  <Input
                    value={form.unitCode} required inputMode="text" placeholder="0x00"
                    onChange={(e) => set('unitCode', maskCode(e.target.value))}
                  />
                </Field>
              </div>
              <div className="flex gap-3">
                {/* Hidden for a listening endpoint (tcp/udp server, any DDS
                    side): core never reads this address for one, it only
                    binds `local_ip` and learns its peer from who connects.
                    Multicast keeps it always, relabeled -- the group address
                    is needed by senders and receivers alike. */}
                {remoteIpVisible(form) && (
                  <Field label={remoteIpLabel(form)}>
                    <Input
                      value={form.ip} required inputMode="numeric" placeholder="0.0.0.0"
                      onChange={(e) => set('ip', maskIp(e.target.value, form.ip))}
                    />
                  </Field>
                )}
                <Field label="Local IP">
                  <Input
                    value={form.local_ip} inputMode="numeric" placeholder="0.0.0.0"
                    onChange={(e) => set('local_ip', maskIp(e.target.value, form.local_ip))}
                  />
                </Field>
              </div>
            </Section>

            <Section
              title="Connected units"
              hint={
                showSharedStructures
                  ? "each unit's own unitCode selects its parse layout"
                  : "each unit's own unitCode selects its parse layout — and its own IRS structures"
              }
              action={
                <AddButton
                  onClick={() => set('peers', [...form.peers, { ...BLANK_PEER }])}
                >
                  Add connected unit
                </AddButton>
              }
            >
              {form.peers.map((peer, index) => (
                <div
                  key={index}
                  className={cx(
                    'flex flex-col gap-2',
                    // Only box them up once each carries its own structures --
                    // a bare row is clearer while there is nothing nested in it.
                    !showSharedStructures &&
                      'rounded-md border border-slate-800 bg-slate-900/40 p-2',
                  )}
                >
                  <div className="flex items-end gap-2">
                    <Field label={index === 0 || !showSharedStructures ? 'Name' : undefined}>
                      <Input
                        value={peer.name} required placeholder="Insert connected unit name"
                        onChange={(e) => setPeer(index, 'name', e.target.value)}
                        className="!font-sans"
                      />
                    </Field>
                    <Field
                      label={index === 0 || !showSharedStructures ? 'Port' : undefined}
                      className="max-w-24"
                    >
                      <Input
                        type="number" min={0} max={65535} value={peer.port} required placeholder="—"
                        onChange={(e) => setPeer(index, 'port', e.target.value)}
                      />
                    </Field>
                    <Field
                      label={index === 0 || !showSharedStructures ? 'unitCode' : undefined}
                      className="max-w-24"
                    >
                      <Input
                        value={peer.unitCode} required placeholder="0x00"
                        onChange={(e) => setPeer(index, 'unitCode', maskCode(e.target.value))}
                      />
                    </Field>
                    <IconButton
                      icon={Trash2} title="Remove connected unit" variant="danger"
                      disabled={form.peers.length === 1}
                      onClick={() => set('peers', form.peers.filter((_, i) => i !== index))}
                      className="mb-px"
                    />
                  </div>

                  {!showSharedStructures && (
                    <StructureList
                      label="IRS structures for this link"
                      entries={peer.structures ?? ['']}
                      onChange={(update) => setPeerStructures(index, update)}
                      onBrowse={(entryIndex) => browseForStructureFile(entryIndex, index)}
                    />
                  )}

                  {showEcho && (
                    <PeerEchoFields peer={peer} onChange={(key, value) => setPeer(index, key, value)} />
                  )}
                </div>
              ))}
            </Section>

            {/* Only shown when ONE list can honestly describe the whole
                connection: a single link, or multicast where one sender fans
                out to many receivers over a shared IRS. With several
                point-to-point units each declares its own, above. */}
            {showSharedStructures && (
              <Section
                title="IRS structures"
                badge={<Badge tone="amber">required</Badge>}
                hint={
                  FANS_OUT.has(form.protocol) && form.peers.length > 1
                    ? 'shared by every receiver — module paths under IRS.Structures, e.g. Test.test_messages'
                    : 'module paths under IRS.Structures, e.g. Test.test_messages'
                }
                action={
                  <AddButton onClick={() => set('structures', [...form.structures, ''])}>
                    Add module
                  </AddButton>
                }
              >
                <StructureList
                  entries={form.structures}
                  onChange={(update) => set('structures', update(form.structures))}
                  onBrowse={(index) => browseForStructureFile(index)}
                />
              </Section>
            )}

            {/* Point-to-point heartbeat only: hidden for multicast/DDS. */}
            {showEcho && (
              <Section title="Echo settings" hint="optional — timeout must exceed interval">
                <div className="flex gap-3">
                  <Field label="echo opCode" hint="decimal or 0x hex">
                    <Input value={form.echo_opcode} placeholder="—"
                           onChange={(e) => set('echo_opcode', maskCode(e.target.value))} />
                  </Field>
                  <Field label="interval (s)">
                    <Input type="number" step="any" value={form.echo_interval} placeholder="1.0"
                           onChange={(e) => set('echo_interval', e.target.value)} />
                  </Field>
                  <Field label="timeout (s)">
                    <Input type="number" step="any" value={form.echo_timeout} placeholder="5.0"
                           onChange={(e) => set('echo_timeout', e.target.value)} />
                  </Field>
                </div>
              </Section>
            )}

            {error && (
              <div className="flex items-start gap-2 rounded-lg border border-rose-900/60 bg-rose-950/30 p-2.5">
                <AlertCircle size={14} className="mt-px shrink-0 text-rose-400" />
                <pre className="min-w-0 whitespace-pre-wrap font-mono text-[11px] text-rose-300">
                  {error}
                </pre>
              </div>
            )}
          </div>

          <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-slate-800 bg-slate-900 px-4 py-3">
            <Button onClick={onClose}>Cancel</Button>
            <Button
              type="submit" variant="primary" disabled={busy}
              icon={busy ? Loader2 : Save}
              className={cx(busy && '[&_svg]:animate-spin')}
            >
              {busy ? 'Working…' : initial ? 'Save changes' : 'Create connection'}
            </Button>
          </footer>
        </form>
      </div>

      {/* Browser-mode fallback for Browse. Rendered inside this modal so it
          layers above it; the desktop shell never reaches here. */}
      {picking && (
        <FilePickerModal
          mode="open"
          title="Select an IRS structures file"
          suffix=".py"
          onPick={(path) => applyStructurePath(path, picking)}
          onClose={() => setPicking(null)}
        />
      )}
    </div>
  );
}

function Section({ title, hint, badge, action, children }) {
  return (
    <fieldset className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <legend className="flex items-center gap-2 px-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
        {title}
        {badge}
      </legend>
      {hint && <p className="mb-2 text-[10px] text-slate-500">{hint}</p>}
      <div className="flex flex-col gap-2.5">{children}</div>
      {action && <div className="mt-2.5">{action}</div>}
    </fieldset>
  );
}

/**
 * The editable list of IRS structures modules, used at both levels: once per
 * peer when each link has its own, or once for the connection when a single
 * list legitimately covers it.
 */
function StructureList({ label, entries, onChange, onBrowse }) {
  const rows = entries.length ? entries : [''];
  return (
    <div className="flex flex-col gap-1.5">
      {label && (
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-medium uppercase tracking-wider text-slate-500">
            {label}
          </span>
          <AddButton onClick={() => onChange((list) => [...list, ''])}>Add module</AddButton>
        </div>
      )}
      {rows.map((entry, index) => (
        <div key={index} className="flex items-end gap-2">
          <Field>
            {/* Abbreviated while idle so a browsed absolute path does not push
                its only meaningful part out of view; focusing reveals the real
                value, which is what gets submitted. */}
            <PathInput
              value={entry} required placeholder="Test.test_messages"
              onChange={(e) =>
                onChange((list) => list.map((s, i) => (i === index ? e.target.value : s)))
              }
            />
          </Field>
          {/* Always offered: with no pywebview the click opens the in-app
              picker instead, rather than the button vanishing. */}
          <IconButton
            icon={FolderOpen} title="Browse for a structures file"
            onClick={() => onBrowse(index)}
          />
          <IconButton
            icon={Trash2} title="Remove module" variant="danger"
            disabled={rows.length === 1}
            onClick={() => onChange((list) => list.filter((_, i) => i !== index))}
          />
        </div>
      ))}
    </div>
  );
}

/**
 * One peer's echo override -- the same three keys as the connection-level
 * Echo settings section, just scoped to this link. Blank fields fall back to
 * the connection's default (`EchoSettings.resolve`, core/connections/config.py);
 * a peer may override just one key (e.g. only the opcode) and still inherit
 * the rest.
 */
function PeerEchoFields({ peer, onChange }) {
  return (
    <div className="flex flex-col gap-1.5 border-t border-slate-800/70 pt-2">
      <span className="text-[10px] font-medium uppercase tracking-wider text-slate-500">
        Echo override{' '}
        <span className="normal-case text-slate-600">(blank = connection default)</span>
      </span>
      <div className="flex gap-2">
        <Field label="echo opCode" hint="decimal or 0x hex">
          <Input
            value={peer.echo_opcode ?? ''} placeholder="—"
            onChange={(e) => onChange('echo_opcode', maskCode(e.target.value))}
          />
        </Field>
        <Field label="interval (s)">
          <Input
            type="number" step="any" value={peer.echo_interval ?? ''} placeholder="—"
            onChange={(e) => onChange('echo_interval', e.target.value)}
          />
        </Field>
        <Field label="timeout (s)">
          <Input
            type="number" step="any" value={peer.echo_timeout ?? ''} placeholder="—"
            onChange={(e) => onChange('echo_timeout', e.target.value)}
          />
        </Field>
      </div>
    </div>
  );
}

function AddButton({ onClick, children }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800/60 px-2 py-1 text-[10px] font-medium text-slate-300 transition-colors hover:border-slate-600 hover:bg-slate-800 hover:text-slate-100 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500"
    >
      <Plus size={11} />
      {children}
    </button>
  );
}
