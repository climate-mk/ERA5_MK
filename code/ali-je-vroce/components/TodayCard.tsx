import { Show, For, createSignal, lazy, Suspense } from "solid-js";
import type { TodayStatus, Last7, SiteMeta, RankInfo } from "../../types/index.ts";
import { TodayFlag } from "./TodayFlag.tsx";

const TodayGauge = lazy(() => import("../charts/TodayGauge.tsx").then(m => ({ default: m.TodayGauge })));

const CATEGORIES: Record<string, string> = {
  freezing: "Zmrzujoče",
  cold:     "Hladno",
  nope:     "Normalno",
  hot:      "Vroče",
  hell:     "Peklensko",
};

const CAT_DESCS: Record<string, string> = {
  freezing: "Med najhladnejšimi {d} v naših {record_years} letih zapisov.",
  cold:     "Hladneje od večine izmerjenih {d}.",
  nope:     "Točno takšno, kot {d} v {country} ponavadi je.",
  hot:      "Med najtoplejšimi {d} v naših zapisih.",
  hell:     "Izjemna vročina — vrh 5 % vseh {d} od {year_min}.",
};

function catDesc(catKey: string, r: TodayStatus): string {
  const tpl = CAT_DESCS[catKey] ?? "";
  const d = r.day_label ?? "";
  const yearMin = r.year_min ?? 1950;
  const yearMax = r.year_max ?? new Date().getFullYear();
  return tpl
    .replace("{d}", d)
    .replace("{year_min}", String(yearMin))
    .replace("{country}", "Slovenija")
    .replace("{record_years}", String(yearMax - yearMin + 1));
}

interface Props {
  data:  TodayStatus;
  last7: Last7 | undefined;
  meta:  SiteMeta;
}

export function TodayCard(props: Props) {
  const r = () => props.data;
  const catKey = () => r().category_key ?? "nope";

  return (
    <Show when={r().available} fallback={<UnavailableCard />}>
      <div class="space-y-6">

        {/* ── Main row: gauge | flag + cat + desc | percentile ── */}
        <div class="flex flex-col sm:flex-row items-center gap-6">

          {/* Gauge */}
          <Suspense fallback={<div style={{ width: "230px", height: "116px" }} class="animate-pulse bg-[var(--color-paper-2)] rounded" />}>
            <TodayGauge data={r()} />
          </Suspense>

          {/* Divider */}
          <div class="hidden sm:block w-px self-stretch bg-[var(--color-rule)]" />

          {/* Flag + category name + description */}
          <div class="flex-1 flex flex-col items-center sm:items-start gap-2 text-center sm:text-left min-w-0">
            <div class="flex items-center gap-3 flex-wrap justify-center sm:justify-start">
              <TodayFlag catKey={catKey()} />
              <span
                class="text-[34px] font-semibold tracking-tight leading-none"
                style={{ color: r().color }}
              >
                {CATEGORIES[catKey()] ?? catKey()}
              </span>
              <Show when={r().rank_info}>
                {(ri) => (
                  <RankBadge
                    info={ri()}
                    dayLabel={r().day_label ?? ""}
                  />
                )}
              </Show>
            </div>
            <p class="text-sm text-[var(--color-ink-soft)] max-w-sm">
              {catDesc(catKey(), r())}
            </p>
          </div>

          {/* Divider */}
          <div class="hidden sm:block w-px self-stretch bg-[var(--color-rule)]" />

          {/* Percentile */}
          <PercentileDisplay pct={r().percentile ?? 0} nSamples={r().n_samples ?? 0} />
        </div>

        {/* ── Last 7 days ── */}
        <Show when={props.last7?.available && (props.last7?.days.length ?? 0) > 0}>
          <Last7Chart days={props.last7!.days} />
        </Show>

        {/* ── Explain footer ── */}
        <p class="text-xs text-[var(--color-ink-soft)] border-t border-dashed border-[var(--color-rule)] pt-4">
          {r().loc
            ? `Temperatura na postaji ${r().loc!.replace(/_/g, " ")}, razvrstena glede na zapise ERA5-Land od leta ${r().year_min} za isto ±7-dnevno okno.`
            : `Današnji vrh je najvišja napovedana temperatura na vseh 18 postajah, razvrstena glede na zapise ERA5-Land od leta ${r().year_min} za isto ±7-dnevno okno.`
          }
        </p>
      </div>
    </Show>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────────


function PercentileDisplay(props: { pct: number; nSamples: number }) {
  return (
    <div class="flex flex-col items-center gap-1 flex-shrink-0 min-w-[72px]">
      <div class="text-[40px] font-bold leading-none text-[var(--color-accent)]">
        {props.pct.toFixed(0)}.
      </div>
      <div class="text-[10px] uppercase tracking-[0.08em] text-[var(--color-ink-soft)] font-semibold font-mono">
        percentil
      </div>
      <div class="text-[10px] text-[var(--color-ink-soft)] opacity-75 font-mono">
        {props.nSamples.toLocaleString()} vzorcev
      </div>
    </div>
  );
}

function RankBadge(props: { info: RankInfo; dayLabel: string }) {
  const [open, setOpen] = createSignal(false);
  const isHot = () => props.info.direction === "hot";

  const label = () =>
    isHot()
      ? `#${props.info.rank} najtoplejši ${props.dayLabel}`
      : `#${props.info.rank} najhladnejši ${props.dayLabel}`;

  return (
    <div class="relative">
      <button
        class="font-mono text-[11px] font-semibold tracking-wide px-2.5 py-1 rounded-full cursor-pointer border transition-[filter] hover:brightness-95"
        style={
          isHot()
            ? { background: "rgba(150,44,26,0.10)", border: "1px solid rgba(150,44,26,0.35)", color: "#962c1a" }
            : { background: "rgba(58,90,138,0.10)", border: "1px solid rgba(58,90,138,0.35)", color: "#3a5a8a" }
        }
        onClick={() => setOpen((v) => !v)}
      >
        {label()}
      </button>

      <Show when={open()}>
        {/* Backdrop */}
        <div class="fixed inset-0 z-10" onClick={() => setOpen(false)} />

        {/* Popup */}
        <div class="absolute top-full mt-2 left-0 z-20 bg-[var(--color-card)] border border-[var(--color-rule)] rounded-xl shadow-lg p-4 min-w-[220px]">
          <div class="text-[10px] font-semibold uppercase tracking-widest text-[var(--color-ink-soft)] mb-3">
            {isHot() ? `Najtoplejši ${props.dayLabel}` : `Najhladnejši ${props.dayLabel}`}
          </div>
          <div class="space-y-2">
            <For each={props.info.top5}>
              {(entry) => (
                <div
                  class="flex justify-between items-center text-sm gap-4"
                  classList={{ "font-semibold": !!entry.is_today }}
                >
                  <span class={entry.is_today ? "text-[var(--color-ink)]" : "text-[var(--color-ink-soft)]"}>
                    {entry.date.slice(0, 4)}
                    <Show when={entry.is_today}>
                      {" "}
                      <span class="text-[10px] font-normal text-[var(--color-accent)]">danes</span>
                    </Show>
                  </span>
                  <span class="font-mono text-[var(--color-ink)]">{entry.temp.toFixed(1)} °C</span>
                </div>
              )}
            </For>
          </div>
        </div>
      </Show>
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
    <div class="text-[var(--color-ink-soft)]">Podatki za ta dan niso na voljo.</div>
  );
}
