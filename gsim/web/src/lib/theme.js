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
