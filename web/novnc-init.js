// noVNC is an ES module with a default export (the RFB class). The rest of the SPA
// is classic scripts, so expose it as a global. Loaded as a module from 'self' so it
// passes the CSP (an inline module would be blocked by script-src 'self').
import RFB from './vendor/novnc/rfb.js';
// the +esm bundle's default export is the module namespace ({ default: RFBClass }) —
// unwrap to the actual constructor.
window.RFB = (RFB && typeof RFB.default === 'function') ? RFB.default : RFB;
