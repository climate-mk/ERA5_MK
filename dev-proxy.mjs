// dev-proxy.mjs — used by Vite in local dev to proxy API calls
// The Solid components reference VITE_SIDECAR_URL and VITE_DATASETTE_URL.
// In dev, Vite can proxy requests so the browser never needs to know the ports.

/** @type {import('vite').UserConfig} */
export const proxyConfig = {
  "/api/live": {
    target: "http://localhost:5052",
    changeOrigin: true,
  },
  "/datasette": {
    target: "http://localhost:8001",
    changeOrigin: true,
    rewrite: (path) => path.replace(/^\/datasette/, ""),
  },
};
