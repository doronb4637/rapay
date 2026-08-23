/**
 * One place that turns a log entry's `timestamp` into text.
 *
 * There used to be two. `Console.jsx` passed `{hour12: false}` and
 * `Inspector.jsx` called `toLocaleTimeString()` bare, so the same instant read
 * as `14:27:07` in the list and `2:27:07 PM` in the panel two hundred pixels
 * away.
 *
 * Both now show milliseconds, because this app's traffic is sub-second by
 * design: the Behaviours panel schedules at 0.5s and `App.jsx` documents
 * coalescing for intervals "well under a frame, e.g. 0.01s". At that rate a
 * seconds-resolution clock cannot order two adjacent rows -- which is exactly
 * the moment someone is reading it.
 */

const CLOCK = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

/** `14:27:07.318` -- fixed width, so a column of these cannot jitter. */
export function formatTime(seconds) {
  const date = new Date(seconds * 1000);
  const millis = String(date.getMilliseconds()).padStart(3, '0');
  return `${CLOCK.format(date)}.${millis}`;
}

/**
 * The gap to the previous entry in the same pane, as text.
 *
 * This is the number you actually want when chasing an echo timeout or
 * checking that a 0.5s behaviour is really firing at 0.5s -- and it is the one
 * thing an absolute clock cannot show you at a glance. `null` for the first
 * row, which has nothing to be relative to.
 */
export function formatDelta(seconds, previousSeconds) {
  if (previousSeconds === null || previousSeconds === undefined) return null;
  const millis = (seconds - previousSeconds) * 1000;
  if (!Number.isFinite(millis) || millis < 0) return null;
  if (millis < 1000) return `+${Math.round(millis)}ms`;
  if (millis < 60000) return `+${(millis / 1000).toFixed(millis < 10000 ? 2 : 1)}s`;
  return `+${Math.round(millis / 60000)}m`;
}

/** `0x1003` -- the form every IRS document and every log row uses. */
export function hex(value, digits = 4) {
  return `0x${Number(value).toString(16).toUpperCase().padStart(digits, '0')}`;
}

/** What a hex code's tooltip says, so the decimal stays one hover away rather
 *  than permanently doubling the width of the densest strip on the panel. */
export function hexTitle(value, label) {
  return `${label}: ${hex(value)} · ${value} decimal`;
}
