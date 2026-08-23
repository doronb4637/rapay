/**
 * Small persisted UI preferences -- panel widths, collapsed sections.
 *
 * Deliberately separate from application state: nothing here is worth a round
 * trip to the server, and losing it costs the user a drag, not their work. Every
 * read and write is guarded because `localStorage` throws outright in a private
 * window rather than returning null.
 */
const PREFIX = 'gsim.';

export function readPref(key, fallback) {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (raw === null) return fallback;
    const parsed = JSON.parse(raw);
    return parsed === null || parsed === undefined ? fallback : parsed;
  } catch {
    return fallback;
  }
}

export function writePref(key, value) {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    /* not persisting is survivable */
  }
}

/** Keep a number inside `[min, max]` -- used for every stored panel width, so a
 *  stale value from a wider monitor cannot strand a panel off screen. */
export function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}
