/** Thin fetch wrapper. In the packaged desktop app the UI is same-origin. */
const BASE = import.meta.env.VITE_API_BASE ?? '';

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    // FastAPI returns {detail: str} for HTTPException and
    // {detail: [{loc, msg}, ...]} for validation errors -- flatten both so the
    // modal can show the real reason from core instead of "request failed".
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = Array.isArray(body.detail)
        ? body.detail.map((e) => `${e.loc.slice(1).join('.')}: ${e.msg}`).join('\n')
        : body.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

/** Build a query string from the entries that actually have a value. */
function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, value);
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

export const api = {
  listConnections: () => request('/api/connections'),
  createConnection: (body) => request('/api/connections', { method: 'POST', body: JSON.stringify(body) }),
  updateConnection: (id, body) => request(`/api/connections/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  // One call per saved connection when Loading a session file -- `body` is
  // `{name, config, autostart}`, core-shaped, from `lib/sessionFile.js`.
  importConnection: (body) => request('/api/connections/import', { method: 'POST', body: JSON.stringify(body) }),
  deleteConnection: (id) => request(`/api/connections/${id}`, { method: 'DELETE' }),
  start: (id) => request(`/api/connections/${id}/start`, { method: 'POST' }),
  stop: (id) => request(`/api/connections/${id}/stop`, { method: 'POST' }),

  // Scoped by DESTINATION: a structures file describes one link, so the same
  // opCode can mean different layouts on two peers of one connection.
  messages: (id, unitName) =>
    request(`/api/connections/${id}/messages${query({ unit_name: unitName })}`),
  messageSchema: (id, opCode, unitName) =>
    request(`/api/connections/${id}/messages/${opCode}/schema${query({ unit_name: unitName })}`),
  // `namespace` comes off the log entry itself, so a received message is
  // rendered against the exact layout it was decoded with.
  schemaByUnit: (unitCode, opCode, namespace) =>
    request(`/api/schema/${unitCode}/${opCode}${query({ namespace })}`),
  send: (id, body) => request(`/api/connections/${id}/send`, { method: 'POST', body: JSON.stringify(body) }),
  logs: (id, direction) => request(`/api/connections/${id}/logs/${direction}`),
  // Process-wide log history: a send and its matching receive belong to two
  // different connections, so the console backfills globally.
  allLogs: (direction) => request(`/api/logs/${direction}`),
  // Server-side, so cleared entries do not come back on the next refresh --
  // these buffers are what allLogs() backfills from.
  clearLogs: (direction) => request(`/api/logs/${direction}`, { method: 'DELETE' }),

  // Behaviours: scheduled sending. Upsert keyed by (connection, unit, opCode) --
  // one schedule per message route, so PUT replaces rather than stacking.
  behaviours: () => request('/api/behaviours'),
  setBehaviour: (connectionName, body) =>
    request(`/api/connections/${connectionName}/behaviours`, { method: 'PUT', body: JSON.stringify(body) }),
  startBehaviour: (id) => request(`/api/behaviours/${id}/start`, { method: 'POST' }),
  stopBehaviour: (id) => request(`/api/behaviours/${id}/stop`, { method: 'POST' }),
  deleteBehaviour: (id) => request(`/api/behaviours/${id}`, { method: 'DELETE' }),

  // Filesystem, for the in-app picker used when no native dialog exists
  // (`--server` + browser tab). The server browses because the browser refuses
  // to hand over real paths, and real paths are exactly what core needs.
  fileRoots: () => request('/api/files/roots'),
  listFiles: (path, suffix) => request(`/api/files/list${query({ path, suffix })}`),
  readFile: (path) => request(`/api/files/read${query({ path })}`),
  saveFile: (path, contents) =>
    request('/api/files/save', { method: 'POST', body: JSON.stringify({ path, contents }) }),
};

/**
 * Live log/state feed. Reconnects on drop; the server replays a snapshot.
 *
 * `onStatus(connected)` reports whether the feed is actually up. Worth
 * surfacing because a dead socket is otherwise INVISIBLE: the HTTP API keeps
 * answering, so the app looks online while silently receiving no live
 * messages, no connection-state changes and no clear broadcasts. Every action
 * still works over HTTP, but nothing pushed ever arrives, which reads as "the
 * UI is stale/ignoring me" rather than "the feed dropped".
 */
export function openEventStream(onEvent, onStatus) {
  let socket;
  let closed = false;

  const connect = () => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const host = BASE ? BASE.replace(/^https?:\/\//, '') : location.host;
    socket = new WebSocket(`${proto}://${host}/ws/events`);
    socket.onopen = () => onStatus?.(true);
    socket.onmessage = (event) => onEvent(JSON.parse(event.data));
    socket.onclose = () => {
      onStatus?.(false);
      if (!closed) setTimeout(connect, 1000);
    };
  };
  connect();

  return () => {
    closed = true;
    socket?.close();
  };
}
