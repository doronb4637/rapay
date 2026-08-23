import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// `base: './'` keeps asset URLs relative so the built bundle works when
// FastAPI serves it from StaticFiles in the packaged desktop app.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: './',
  build: { outDir: 'ui_dist' }, // Keeps Vite output separate from PyInstaller's dist/
  server: {
    // Dev only: Vite serves the UI, FastAPI serves the API.
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/ws': { target: 'ws://127.0.0.1:8765', ws: true },
    },
  },
});
