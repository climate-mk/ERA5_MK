// dev-proxy.mjs — used by Vite in local dev to proxy API calls
// The Solid components reference VITE_SIDECAR_URL and VITE_DATASETTE_URL.
// In dev, Vite can proxy requests so the browser never needs to know the ports.

/** @type {import('vite').UserConfig} */
export const proxyConfig = {
  "/api/live": {
    target: "http://localhost:5052",
    changeOrigin: true,
  },
  // Fallback: remaining /api/* routes go to the main Flask API (port 5050)
  // This covers spei_heatmap, spei_station_seasonal, tropical_days/nights, etc.
  "/api": {
    target: "http://localhost:5050",
    changeOrigin: true,
  },
  "/datasette": {
    target: "http://localhost:8001",
    changeOrigin: true,
    rewrite: (path) => path.replace(/^\/datasette/, ""),
  },
  // Static data files (flood PNGs, flood-stats.json, etc.) served by Flask
  "/data": {
    target: "http://localhost:5050",
    changeOrigin: true,
  },
  // Static files (topo JSON, highcharts maps data, etc.) served by Flask at root
  "/js": {
    target: "http://localhost:5050",
    changeOrigin: true,
  },
};
