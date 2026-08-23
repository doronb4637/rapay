/**
 * Theme selection: dark (the default) or light.
 *
 * The switch itself is one attribute on <html>; every colour follows from the
 * `:root[data-theme='light']` block in styles.css, which redefines Tailwind's
 * `--color-*` variables. No component reads this.
 *
 * Applied to `document.documentElement` rather than held only in React state
 * so `<body>`'s own background follows -- body sits outside the React tree, and
 * a dark body behind a light app shows through as a flash on load and as a
 * band under short content.
 */

const STORAGE_KEY = 'gsim.theme';

export const THEMES = ['dark', 'light'];

/** Stored choice, else the OS preference, else dark. */
export function initialTheme() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (THEMES.includes(saved)) return saved;
  } catch {
    /* storage blocked (private mode); fall through to the OS preference */
  }
  const prefersLight =
    typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-color-scheme: light)').matches;
  return prefersLight ? 'light' : 'dark';
}

export function applyTheme(theme) {
  const root = document.documentElement;
  // Dark is the base stylesheet, so it is the ABSENCE of the attribute rather
  // than a value -- that way the default costs no overrides at all.
  if (theme === 'light') root.setAttribute('data-theme', 'light');
  else root.removeAttribute('data-theme');
  // Lets the browser paint native widgets (scrollbars, form controls, the
  // canvas behind the page) to match instead of assuming dark.
  root.style.colorScheme = theme === 'light' ? 'light' : 'dark';
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* not persisting is survivable; the session still looks right */
  }
}

/**
 * Stamp `data-font-sans="inter"` on <html> once Inter has actually resolved.
 *
 * `styles.css` applies Inter's `cv02/cv03/cv04/ss01` character alternates, and
 * those are Inter's own -- on Segoe UI or any other fallback they are simply
 * ignored. Declaring them unconditionally (which is what this file used to do
 * by omission) meant the stylesheet asserted a typographic decision that was
 * not in effect on any machine without Inter installed. Asking first makes the
 * declaration true wherever it applies.
 *
 * `document.fonts.check` answers for locally installed families as well as
 * loaded ones, which is the point: `@font-face` tries `local('Inter')` before
 * any download, so "is Inter available" is the only question worth asking.
 * `document.fonts.ready` is awaited first so a woff2 still in flight is not
 * mistaken for an absent family.
 */
export function detectSansFace() {
  const stamp = () => {
    try {
      if (document.fonts?.check?.('12px Inter')) {
        document.documentElement.setAttribute('data-font-sans', 'inter');
      }
    } catch {
      /* no Font Loading API; the fallback stack is already correct */
    }
  };
  if (document.fonts?.ready) document.fonts.ready.then(stamp, stamp);
  else stamp();
}
