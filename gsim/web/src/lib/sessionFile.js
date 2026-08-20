/**
 * Save/Load session file: the envelope written by the Sidebar's Save button
 * and read back by Load. One entry per connection, each carrying exactly what
 * `POST /api/connections/import` needs (`gsim/api/models.ConnectionImport`) --
 * `config` is core's own JSON shape (`connection.config` from `GET
 * /api/connections`), not the modal's stricter form model, so importing a
 * saved file is a lossless round trip rather than a second, weaker
 * constructor. See `ConnectionImport`'s docstring for why.
 */

/** Build the envelope Save writes. */
export function buildSessionExport(connections) {
  return {
    version: 1,
    exported_at: new Date().toISOString(),
    connections: connections.map((connection) => ({
      name: connection.name,
      autostart: connection.running,
      config: connection.config,
    })),
  };
}

/**
 * Parse a Load'ed file's text into the array `api.importConnection` expects,
 * one call per entry. Throws with a message fit to show the user directly --
 * this runs before anything reaches the network, so it is the only chance to
 * catch "wrong file" before the API does.
 */
export function parseSessionImport(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch (err) {
    throw new Error(`not a valid GSim connections file (bad JSON): ${err.message}`);
  }
  // A bare array is accepted too, for a hand-written file -- the envelope's
  // `version`/`exported_at` are provenance, not load-bearing.
  const entries = Array.isArray(data) ? data : data?.connections;
  if (!Array.isArray(entries)) {
    throw new Error('not a GSim connections file: expected {"connections": [...]}');
  }
  return entries.map((entry, index) => {
    if (!entry || typeof entry !== 'object' || !entry.name || !entry.config) {
      throw new Error(`entry ${index + 1}: missing "name" or "config"`);
    }
    return { name: entry.name, config: entry.config, autostart: entry.autostart ?? true };
  });
}

// A browser-download fallback used to live here. It is gone on purpose: a
// download lands wherever the browser puts downloads, under a name the user
// cannot choose, which is exactly the complaint it caused. Browser mode now
// writes through `POST /api/files/save` after choosing a real path in
// `FilePickerModal`, so both shells save to the same place the same way.
