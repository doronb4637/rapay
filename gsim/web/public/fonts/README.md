# Fonts

`src/styles.css` names **Inter** (sans) and **JetBrains Mono** (mono) and
declares an `@font-face` for each. Both rules try `local()` first, so if either
family is installed on the machine it is used with no download at all.

Drop the woff2 files here to guarantee them on a machine that has neither —
the packaged app is an offline PyWebView window, so a Google Fonts link would
just fail and there is nothing to fetch from:

    InterVariable.woff2      https://github.com/rsms/inter/releases  (Inter/web)
    JetBrainsMono.woff2      https://github.com/JetBrains/JetBrainsMono/releases

Filenames matter — they are what `@font-face` asks for. Anything in
`web/public/` is served at the site root, so these resolve as `/fonts/<name>`.

With neither installed nor present, the `src:` entries resolve to nothing, the
browser skips them, and the fallback stack in `@theme` takes over (Segoe UI and
Cascadia Mono on Windows). Nothing breaks; the app just is not in Inter.

`lib/theme.js` asks `document.fonts` whether Inter actually resolved and stamps
`data-font-sans="inter"` on `<html>` when it did. That attribute is what gates
the `cv02/cv03/cv04/ss01` character alternates in `styles.css`, which are
Inter's own and mean nothing to any other family.
