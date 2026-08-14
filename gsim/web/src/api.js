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

export const api = {
  listConnections: () => request('/api/connections'),
  createConnection: (body) => request('/api/connections', { method: 'POST', body: JSON.stringify(body) }),
  updateConnection: (id, body) => request(`/api/connections/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  deleteConnection: (id) => request(`/api/connections/${id}`, { method: 'DELETE' }),
  start: (id) => request(`/api/connections/${id}/start`, { method: 'POST' }),
  stop: (id) => request(`/api/connections/${id}/stop`, { method: 'POST' }),

  messages: (id) => request(`/api/connections/${id}/messages`),
  messageSchema: (id, opCode) => request(`/api/connections/${id}/messages/${opCode}/schema`),
  schemaByUnit: (unitCode, opCode) => request(`/api/schema/${unitCode}/${opCode}`),
  send: (id, body) => request(`/api/connections/${id}/send`, { method: 'POST', body: JSON.stringify(body) }),
  logs: (id, direction) => request(`/api/connections/${id}/logs/${direction}`),
  // Process-wide log history: a send and its matching receive belong to two
  // different connections, so the console backfills globally.
  allLogs: (direction) => request(`/api/logs/${direction}`),
};

/** Live log/state feed. Reconnects on drop; the server replays a snapshot. */
export function openEventStream(onEvent) {
  let socket;
  let closed = false;

  const connect = () => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const host = BASE ? BASE.replace(/^https?:\/\//, '') : location.host;
    socket = new WebSocket(`${proto}://${host}/ws/events`);
    socket.onmessage = (event) => onEvent(JSON.parse(event.data));
    socket.onclose = () => {
      if (!closed) setTimeout(connect, 1000);
    };
  };
  connect();

  return () => {
    closed = true;
    socket?.close();
  };
}
