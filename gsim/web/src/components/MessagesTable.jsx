/**
 * Messages workspace: what the selected connection is permitted to SEND.
 *
 * Rows come from the GLOBAL REGISTRY keyed by this connection's OWN unitCode --
 * the same code `irs_to_bytes` is called with on the way out, so this is
 * exactly the set that will encode. A row click loads it into the Inspector.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Inbox, Search, Send } from 'lucide-react';
import { api } from '../api';
import { Badge, EmptyState, Input, Panel, PanelHeader, cx } from './ui';

export default function MessagesTable({ connectionId, activeOpCode, onCompose, className }) {
  const [messages, setMessages] = useState([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState(null);

  useEffect(() => {
    setQuery('');
    setError(null);
    if (!connectionId) return setMessages([]);
    let cancelled = false;
    api.messages(connectionId).then(
      (next) => !cancelled && setMessages(next),
      (err) => {
        if (cancelled) return;
        setMessages([]);
        setError(err.message);
      },
    );
    return () => { cancelled = true; };
  }, [connectionId]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return messages;
    return messages.filter(
      (message) =>
        message.name.toLowerCase().includes(needle) ||
        message.op_code_hex.toLowerCase().includes(needle),
    );
  }, [messages, query]);

  return (
    <Panel className={className}>
      <PanelHeader title="Messages" icon={Inbox} count={messages.length} />

      <div className="shrink-0 px-2 py-1.5">
        <div className="relative">
          <Search
            size={12}
            className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-slate-600"
          />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter messages…"
            disabled={!connectionId}
            className="!py-1 pl-7 text-[11px]"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {!connectionId ? (
          <EmptyState icon={Inbox}>Select a connection to see the messages it can send.</EmptyState>
        ) : error ? (
          <EmptyState icon={Inbox}>{error}</EmptyState>
        ) : visible.length === 0 ? (
          <EmptyState icon={Inbox}>
            {messages.length === 0
              ? 'No messages registered for this unitCode. Check the connection’s Structures modules.'
              : `Nothing matches “${query}”.`}
          </EmptyState>
        ) : (
          // A list, not a table: this panel now lives in the narrow left
          // column, where four columns of headers would not fit legibly.
          <ul className="flex flex-col gap-0.5 px-1.5 pb-1.5">
            {visible.map((message) => {
              const isActive = message.op_code === activeOpCode;
              return (
                <li key={message.op_code}>
                  <button
                    type="button"
                    onClick={() => onCompose(message.op_code)}
                    className={cx(
                      'group flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left',
                      'transition-colors duration-100',
                      'focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/70',
                      isActive
                        ? 'bg-sky-500/10 ring-1 ring-sky-500/30'
                        : 'hover:bg-slate-800/60',
                    )}
                  >
                    <div className="min-w-0 flex-1">
                      <div
                        className={cx(
                          'truncate text-xs font-medium',
                          isActive ? 'text-sky-300' : 'text-slate-200',
                        )}
                      >
                        {message.name}
                      </div>
                      <div className="font-mono text-[10px] text-slate-500">
                        {message.field_count} field{message.field_count === 1 ? '' : 's'}
                      </div>
                    </div>
                    <Badge tone={isActive ? 'sky' : 'slate'}>{message.op_code_hex}</Badge>
                    <Send
                      size={12}
                      className={cx(
                        'shrink-0 text-sky-400 transition-opacity',
                        isActive ? 'opacity-100' : 'opacity-0 group-hover:opacity-100',
                      )}
                    />
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Panel>
  );
}
