import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { detectSansFace } from './lib/theme';
import './styles.css';

// Stamps <html data-font-sans="inter"> only if Inter actually resolved, which
// is what gates its character alternates in styles.css. Fire-and-forget: it
// settles on the next frame and changes nothing but a font-feature-settings.
detectSansFace();

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
