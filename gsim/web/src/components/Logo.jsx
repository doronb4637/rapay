/**
 * GSim mark.
 *
 * A vector reconstruction of the project badge: a hex shield, broadcast arcs
 * radiating from the top, and an isometric cube carrying the "GS" motif. Drawn
 * as geometry rather than shipped as a bitmap so it stays crisp at the 18px
 * the title bar uses and at any size a splash/about screen might want, and so
 * it inherits theme colours instead of baking in a background.
 *
 * To use the original pixel-art PNG instead: drop it at `web/public/logo.png`
 * and swap the <svg> here for
 *   <img src="./logo.png" width={size} height={size} alt="GSim" />
 * -- everything else (sizing, placement) already flows through `size`.
 */
import React from 'react';

export default function Logo({ size = 18, className }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      role="img"
      aria-label="GSim"
    >
      {/* Hex shield */}
      <path
        d="M32 3 58 17v30L32 61 6 47V17L32 3Z"
        className="stroke-emerald-300/70"
        strokeWidth="3"
        fill="none"
        strokeLinejoin="round"
      />

      {/* Broadcast arcs -- the "signal" half of the badge */}
      <path
        d="M20 24a17 17 0 0 1 24 0"
        className="stroke-emerald-300/45"
        strokeWidth="2.5"
        strokeLinecap="round"
        fill="none"
      />
      <path
        d="M25.5 29a9.5 9.5 0 0 1 13 0"
        className="stroke-emerald-300/70"
        strokeWidth="2.5"
        strokeLinecap="round"
        fill="none"
      />

      {/* Isometric cube: top face, then the two lit/shaded side faces. */}
      <path d="M32 31 43 37.2 32 43.5 21 37.2 32 31Z" className="fill-emerald-200/90" />
      <path d="M21 37.2 32 43.5V56L21 49.7V37.2Z" className="fill-emerald-300/55" />
      <path d="M43 37.2 32 43.5V56l11-6.3V37.2Z" className="fill-emerald-400/35" />

      {/* "G" carved into the left face, "S" into the right -- the GS motif. */}
      <path
        d="M28.5 44.6v6.6M25.3 42.8v6.6M25.3 49.4l3.2 1.8M25.3 42.8l3.2 1.8M28.5 47.9l-1.6-.9"
        className="stroke-slate-900"
        strokeWidth="1.4"
        strokeLinecap="round"
        fill="none"
      />
      <path
        d="M39 44.5l-3 1.7v2l3-1.7v2l-3 1.7"
        className="stroke-slate-900"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}
