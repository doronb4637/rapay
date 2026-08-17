/**
 * In-app file browser, for when the native dialog is not available.
 *
 * The desktop shell has real OS dialogs (`gsim/__main__.py`), and those are
 * used whenever `window.pywebview` exists. This is the `--server` + browser-tab
 * path, where a plain `<input type="file">` cannot help: the browser withholds
 * the real path on purpose, and every path here is either handed to core as a
 * `Structures` entry or written to disk by the server. So the server browses
 * (`/api/files/*`) and this renders the result.
 *
 * One component covers both directions -- `mode="open"` picks an existing file,
 * `mode="save"` picks a directory plus a filename -- because the navigation,
 * filtering and breadcrumb behaviour are identical and only the footer differs.
 */
import React, { useCallback, useEffect, useState } from 'react';
import {
  AlertCircle, ArrowUp, File as FileIcon, Folder, HardDrive, Loader2, Save, X,
} from 'lucide-react';
import { api } from '../api';
import { Button, IconButton, Input, cx } from './ui';

export default function FilePickerModal({
  mode = 'open',            // 'open' | 'save'
  title,
  startPath,
  suffix,                   // e.g. '.py' -- only matching files are listed
  defaultFileName = '',
  onPick,                   // (absolutePath) => void | Promise
  onClose,
}) {
  const [path, setPath] = useState(startPath ?? null);
  const [listing, setListing] = useState(null);
  const [selected, setSelected] = useState(null);
  const [fileName, setFileName] = useState(defaultFileName);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  // Resolve the starting directory from the server when the caller had none --
  // the client cannot know where IRS/Structures lives, and hardcoding a guess
  // would break the moment the repo moves.
  useEffect(() => {
    if (path) return;
    let cancelled = false;
    api.fileRoots().then(
      (roots) => !cancelled && setPath(suffix === '.py' ? roots.structures : roots.configs),
      (err) => !cancelled && setError(err.message),
    );
    return () => { cancelled = true; };
  }, [path, suffix]);

  const load = useCallback(
    (target) => {
      setBusy(true);
      setError(null);
      api.listFiles(target, suffix).then(
        (next) => {
          setListing(next);
          setPath(next.path);
          setSelected(null);
          setBusy(false);
        },
        (err) => {
          setError(err.message);
          setBusy(false);
        },
      );
    },
    [suffix],
  );

  useEffect(() => { if (path) load(path); }, [path, load]);

  useEffect(() => {
    const onKey = (event) => event.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const choose = async (absolutePath) => {
    setBusy(true);
    setError(null);
    try {
      await onPick(absolutePath);
      onClose();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  };

  const confirm = () => {
    if (mode === 'save') {
      const name = fileName.trim();
      if (!name) return setError('Enter a file name.');
      // Server joins with its own separator; a plain join is fine because
      // `listing.path` is already an absolute OS path.
      const sep = listing?.path?.includes('\\') ? '\\' : '/';
      return choose(`${listing.path}${sep}${name}`);
    }
    if (!selected) return setError('Select a file.');
    return choose(selected);
  };

  const activate = (entry) => {
    if (entry.is_dir) return load(entry.path);
    setSelected(entry.path);
    if (mode === 'save') setFileName(entry.name);   // overwrite an existing file
  };

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-slate-950/80 p-6 backdrop-blur-sm"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        onMouseDown={(event) => event.stopPropagation()}
        className="flex h-[32rem] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-2xl shadow-slate-950/70"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-slate-800 px-4 py-3">
          {mode === 'save' ? <Save size={14} className="text-sky-400" /> : <Folder size={14} className="text-sky-400" />}
          <h2 className="text-sm font-semibold text-slate-100">
            {title ?? (mode === 'save' ? 'Save file' : 'Open file')}
          </h2>
          <IconButton icon={X} title="Close" onClick={onClose} className="ml-auto" />
        </header>

        {/* Current location + up. The full path is shown rather than a
            breadcrumb: these paths are the value being chosen, and seeing the
            whole thing is the point. */}
        <div className="flex shrink-0 items-center gap-2 border-b border-slate-800 px-3 py-2">
          <IconButton
            icon={ArrowUp}
            title={listing?.parent ? `Up to ${listing.parent}` : 'At the top'}
            disabled={!listing?.parent || busy}
            onClick={() => load(listing.parent)}
          />
          <HardDrive size={12} className="shrink-0 text-slate-600" />
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-slate-400" title={listing?.path}>
            {listing?.path ?? 'Loading…'}
          </span>
          {busy && <Loader2 size={12} className="shrink-0 animate-spin text-slate-500" />}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
          {!listing ? (
            <p className="p-6 text-center text-xs text-slate-500">Loading…</p>
          ) : listing.entries.length === 0 ? (
            <p className="p-6 text-center text-xs text-slate-500">
              Nothing here{suffix ? ` matching ${suffix}` : ''}.
            </p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {listing.entries.map((entry) => (
                <li key={entry.path}>
                  <button
                    type="button"
                    onDoubleClick={() => activate(entry)}
                    onClick={() => (entry.is_dir ? load(entry.path) : activate(entry))}
                    className={cx(
                      'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left',
                      'transition-colors duration-100',
                      'focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/70',
                      selected === entry.path
                        ? 'bg-sky-500/10 ring-1 ring-sky-500/30'
                        : 'hover:bg-slate-800/60',
                    )}
                  >
                    {entry.is_dir
                      ? <Folder size={13} className="shrink-0 text-sky-400" />
                      : <FileIcon size={13} className="shrink-0 text-slate-500" />}
                    <span
                      className={cx(
                        'truncate text-xs',
                        entry.is_dir ? 'text-slate-200' : 'text-slate-300',
                        selected === entry.path && 'text-sky-300',
                      )}
                    >
                      {entry.name}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {error && (
          <div className="flex shrink-0 items-start gap-2 border-t border-rose-900/60 bg-rose-950/30 px-3 py-2">
            <AlertCircle size={14} className="mt-px shrink-0 text-rose-400" />
            <pre className="min-w-0 whitespace-pre-wrap font-mono text-[11px] text-rose-300">{error}</pre>
          </div>
        )}

        <footer className="flex shrink-0 items-center gap-2 border-t border-slate-800 bg-slate-900 px-4 py-3">
          {mode === 'save' && (
            <Input
              value={fileName}
              placeholder="file-name.json"
              onChange={(event) => setFileName(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && confirm()}
              className="!font-mono"
            />
          )}
          {mode === 'open' && (
            <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-slate-500" title={selected ?? ''}>
              {selected ?? 'Select a file'}
            </span>
          )}
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={busy} onClick={confirm}>
            {mode === 'save' ? 'Save' : 'Open'}
          </Button>
        </footer>
      </div>
    </div>
  );
}
