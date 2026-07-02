import { createSignal, For, Show } from "solid-js";
import type { SeasonHeatmapRow } from "../../types/index.ts";

const SEASON_LABELS: Record<string, string> = {
  Winter: "Zima",
  Spring: "Pomlad",
  Summer: "Poletje",
  Autumn: "Jesen",
};

const CAT_LABELS: Record<string, string> = {
  cold:    "Hladno",
  cool:    "Sveže",
  normal:  "Normalno",
  hot:     "Vroče",
  extreme: "Ekstremno",
};

const CAT_COLORS: Record<string, string> = {
  cold:    "#3a5a8a",
  cool:    "#6c8fb6",
  normal:  "#e7d9b8",
  hot:     "#c25a2c",
  extreme: "#962c1a",
};

const SEASONS = ["Winter", "Spring", "Summer", "Autumn"] as const;

interface Props {
  data: SeasonHeatmapRow[];
}

export function SeasonHeatmap(props: Props) {
  const [hovered, setHovered] = createSignal<SeasonHeatmapRow | null>(null);

  const yearMin = () => Math.min(...props.data.map((r) => r.y));
  const yearMax = () => Math.max(...props.data.map((r) => r.y));

  const years = () => {
    const min = yearMin(), max = yearMax();
    return Array.from({ length: max - min + 1 }, (_, i) => min + i);
  };

  // Lookup: Map<x, Map<y, row>>
  const index = () => {
    const m = new Map<number, Map<number, SeasonHeatmapRow>>();
    for (const row of props.data) {
      if (!m.has(row.x)) m.set(row.x, new Map());
      m.get(row.x)!.set(row.y, row);
    }
    return m;
  };

  return (
    <div class="space-y-3">
      {/* Heatmap grid */}
      <div class="overflow-x-auto">
        <div class="min-w-[280px]">
          <For each={[0, 1, 2, 3]}>
            {(xi) => (
              <div class="flex items-stretch mb-[2px]">
                {/* Season label */}
                <div class="w-16 shrink-0 flex items-center justify-end pr-2 text-xs text-[var(--color-ink-soft)]">
                  {SEASON_LABELS[SEASONS[xi]]}
                </div>
                {/* Cell row */}
                <div class="flex flex-1 gap-[1px]" style={{ height: "20px" }}>
                  <For each={years()}>
                    {(yr) => {
                      const cell = index().get(xi)?.get(yr);
                      return (
                        <div
                          class="flex-1 cursor-default"
                          style={{
                            background: cell?.color ?? "#e0ddd6",
                            opacity:    cell ? "1" : "0.2",
                          }}
                          onMouseEnter={() => setHovered(cell ?? null)}
                          onMouseLeave={() => setHovered(null)}
                        />
                      );
                    }}
                  </For>
                </div>
              </div>
            )}
          </For>

          {/* Year axis: label every decade */}
          <div class="flex ml-16 gap-[1px] mt-1">
            <For each={years()}>
              {(yr) => (
                <div
                  class="flex-1 text-center leading-none text-[var(--color-ink-soft)] overflow-hidden"
                  style={{ "font-size": "9px" }}
                >
                  {yr % 10 === 0 ? String(yr) : ""}
                </div>
              )}
            </For>
          </div>
        </div>
      </div>

      {/* Hover info bar */}
      <div class="min-h-[2rem] flex items-center">
        <Show
          when={hovered()}
          fallback={
            <p class="text-xs text-[var(--color-ink-soft)]">
              Premaknite miško nad celico za podrobnosti
            </p>
          }
        >
          {(cell) => (
            <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
              <span class="font-semibold">
                {cell().y} · {SEASON_LABELS[cell().season]}
              </span>
              <span class="text-[var(--color-ink-soft)]">
                {cell().avg.toFixed(1)} °C
              </span>
              <span class="text-[var(--color-ink-soft)]">
                {cell().percentile.toFixed(0)}. percentil (1950–1980)
              </span>
              <span class="text-[var(--color-ink-soft)]">
                #{cell().rank} od {cell().total}
              </span>
              <span
                class="px-1.5 py-0.5 rounded text-white text-xs font-medium"
                style={{ background: CAT_COLORS[cell().cat] }}
              >
                {CAT_LABELS[cell().cat]}
              </span>
            </div>
          )}
        </Show>
      </div>

      {/* Legend */}
      <div class="flex flex-wrap gap-x-4 gap-y-1">
        <For each={Object.entries(CAT_COLORS)}>
          {([cat, color]) => (
            <div class="flex items-center gap-1.5 text-xs text-[var(--color-ink-soft)]">
              <div class="w-3 h-3 rounded-[1px]" style={{ background: color }} />
              {CAT_LABELS[cat]}
            </div>
          )}
        </For>
      </div>
    </div>
  );
}
