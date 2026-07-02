import { Show, For } from "solid-js";
import type { TodayStatus, Last7, SiteMeta } from "../../types/index.ts";

const CATEGORIES: Record<string, string> = {
  freezing: "Zmrzal",
  cold:     "Hladno",
  nope:     "Normalno",
  hot:      "Vroče",
  hell:     "Peklensko",
};

interface Props {
  data:  TodayStatus;
  last7: Last7 | undefined;
  meta:  SiteMeta;
}

export function TodayCard(props: Props) {
  const r = () => props.data;

  return (
    <Show when={r().available} fallback={<UnavailableCard />}>
      <div class="bg-[var(--color-card)] border border-[var(--color-rule)] rounded-2xl p-6 space-y-6">

        {/* Gauge row */}
        <div class="flex flex-col sm:flex-row items-center gap-6">
          <GaugeMeter pct={r().percentile ?? 0} color={r().color ?? "#e7d9b8"} />

          <div class="flex-1 text-center sm:text-left space-y-1">
            <div class="text-4xl font-bold" style={{ color: r().color }}>
              {CATEGORIES[r().category_key ?? "nope"] ?? r().category_key}
            </div>
            <div class="text-2xl font-mono font-semibold">
              {r().today_temp?.toFixed(1)} °C
            </div>
            <div class="text-sm text-[var(--color-ink-soft)]">
              {r().day_label} · {r().loc ? r().loc!.replace(/_/g, " ") : "Slovenija"}
            </div>
          </div>

          <PercentileDisplay pct={r().percentile ?? 0} nSamples={r().n_samples ?? 0} />
        </div>

        {/* Last-7-days mini chart */}
        <Show when={props.last7?.available && (props.last7?.days.length ?? 0) > 0}>
          <Last7Chart days={props.last7!.days} />
        </Show>

        {/* Explain */}
        <p class="text-xs text-[var(--color-ink-soft)] border-t border-dashed border-[var(--color-rule)] pt-4">
          Današnji vrh primerjamo z ERA5-Land podatki od leta {r().year_min} za isto ±7-dnevno okno.
          Percentil {r().percentile?.toFixed(0)}. pomeni, da je bila temperatura višja kot v{" "}
          {r().percentile?.toFixed(0)}% vseh podobnih dni.
        </p>
      </div>
    </Show>
  );
}

function GaugeMeter(props: { pct: number; color: string }) {
  // Half-donut SVG gauge, 0–100 → 180° arc
  const r = 70, cx = 100, cy = 90;
  const totalArc = Math.PI;
  const angle = (props.pct / 100) * totalArc;
  const x = cx + r * Math.cos(Math.PI - angle);
  const y = cy - r * Math.sin(Math.PI - angle);
  const largeArc = angle > Math.PI / 2 ? 1 : 0;

  return (
    <svg viewBox="0 0 200 100" width="200" height="100" aria-hidden="true">
      {/* Track */}
      <path d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`}
            fill="none" stroke="#e5e2d8" stroke-width="14" stroke-linecap="round" />
      {/* Fill */}
      <Show when={props.pct > 0}>
        <path d={`M ${cx - r} ${cy} A ${r} ${r} 0 ${largeArc} 1 ${x} ${y}`}
              fill="none" stroke={props.color} stroke-width="14" stroke-linecap="round" />
      </Show>
    </svg>
  );
}

function PercentileDisplay(props: { pct: number; nSamples: number }) {
  return (
    <div class="flex flex-col items-center gap-1 min-w-[80px]">
      <div class="text-4xl font-bold text-[var(--color-accent)]">
        {props.pct.toFixed(0)}.
      </div>
      <div class="text-xs uppercase tracking-widest text-[var(--color-ink-soft)]">percentil</div>
      <div class="text-xs text-[var(--color-ink-soft)] opacity-75">{props.nSamples} vzorcev</div>
    </div>
  );
}

function Last7Chart(props: { days: Last7["days"] }) {
  return (
    <div class="space-y-1">
      <div class="text-xs uppercase tracking-widest text-[var(--color-ink-soft)]">Zadnjih 7 dni</div>
      <div class="flex gap-2 items-end h-16">
        <For each={props.days}>
          {(d) => {
            const h = Math.max(8, (d.percentile / 100) * 56);
            return (
              <div class="flex flex-col items-center gap-1 flex-1">
                <div
                  class="w-full rounded-sm"
                  style={{ height: `${h}px`, background: d.color }}
                  title={`${d.day_label}: ${d.today_temp.toFixed(1)}°C (${d.percentile.toFixed(0)}. pct)`}
                />
                <div class="text-[9px] text-[var(--color-ink-soft)] text-center leading-tight">
                  {d.day_label.split(" ")[1]}
                </div>
              </div>
            );
          }}
        </For>
      </div>
    </div>
  );
}

function UnavailableCard() {
  return (
    <div class="bg-[var(--color-card)] border border-[var(--color-rule)] rounded-2xl p-6 text-[var(--color-ink-soft)]">
      Podatki za ta dan niso na voljo.
    </div>
  );
}
