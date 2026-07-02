import { onMount, onCleanup, createEffect } from "solid-js";
import "leaflet/dist/leaflet.css";
import type { SiteMeta } from "../../types/index.ts";

interface Props {
  meta:     SiteMeta;
  loc:      string | null;       // currently selected station name
  onSelect: (loc: string | null) => void;
}

// Elevation → fill colour (matches the original Flask SPA palette)
function elevColor(elev: number): string {
  if (elev > 1500) return "#7bafd4";
  if (elev > 800)  return "#a3c4a0";
  if (elev > 400)  return "#c8b97a";
  return "#c25a2c";
}

export function StationMap(props: Props) {
  let container!: HTMLDivElement;
  let map: import("leaflet").Map | null = null;
  const markers: Map<string, import("leaflet").CircleMarker> = new Map();

  onMount(async () => {
    const L = (await import("leaflet")).default;

    map = L.map(container, {
      center:    [props.meta.map.center_lat, props.meta.map.center_lon],
      zoom:      props.meta.map.zoom,
      zoomControl: true,
      scrollWheelZoom: false,
    });

    // Recalculate size whenever the container resizes (handles initial 0-width case)
    const ro = new ResizeObserver(() => map?.invalidateSize());
    ro.observe(container);
    onCleanup(() => ro.disconnect());

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 13,
    }).addTo(map);

    for (const s of props.meta.stations) {
      const isActive = s.name === props.loc;
      const marker = L.circleMarker([s.lat, s.lon], {
        radius:      isActive ? 10 : 7,
        fillColor:   elevColor(s.elevation),
        color:       isActive ? "#1a1a18" : "#fff",
        weight:      isActive ? 2.5 : 1.5,
        fillOpacity: 0.9,
      })
        .bindTooltip(s.name.replace(/_/g, " "), {
          permanent: false,
          direction: "top",
          offset:    [0, -6],
          className: "station-tooltip",
        })
        .addTo(map);

      marker.on("click", () => {
        // Toggle off if clicking the already-selected station
        props.onSelect(props.loc === s.name ? null : s.name);
      });

      markers.set(s.name, marker);
    }
  });

  // React to loc changes — update marker styles
  createEffect(() => {
    const active = props.loc;
    markers.forEach((marker, name) => {
      const sel = name === active;
      marker.setStyle({
        radius:      sel ? 10 : 7,
        color:       sel ? "#1a1a18" : "#fff",
        weight:      sel ? 2.5 : 1.5,
      });
    });
  });

  onCleanup(() => {
    map?.remove();
    map = null;
    markers.clear();
  });

  return (
    <div
      ref={container}
      class="w-full rounded-xl overflow-hidden border border-[var(--color-rule)]"
      style={{ height: "280px" }}
    />
  );
}
