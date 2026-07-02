import { createSignal, createResource, Show, Suspense, For, lazy } from "solid-js";
import { fetchMeta, fetchTodayStatus, fetchLast7, fetchAnnualTrend } from "./api.ts";
import { TodayCard } from "./components/TodayCard.tsx";
import type { SiteMeta } from "../types/index.ts";

// Lazy-load heavy chart components so the today card renders first
const AnnualTrendChart  = lazy(() => import("./charts/AnnualTrendChart.tsx").then(m => ({ default: m.AnnualTrendChart })));
const DistributionChart = lazy(() => import("./charts/DistributionChart.tsx").then(m => ({ default: m.DistributionChart })));

function addDays(dateStr: string, n: number): string {
  const d = new Date(dateStr + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export function AliJeVroceERA5() {
  const [meta] = createResource<SiteMeta>(fetchMeta);
  return (
    <Show when={meta()} fallback={<Spinner />}>
      {(m) => <Dashboard meta={m()} />}
    </Show>
  );
}

function Dashboard(props: { meta: SiteMeta }) {
  const today = new Date().toISOString().slice(0, 10);
  const [date, setDate] = createSignal(today);
  const [loc,  setLoc]  = createSignal<string | null>(null);

  const [todayData] = createResource(
    () => ({ date: date(), loc: loc() }),
    ({ date, loc }) => fetchTodayStatus(date, loc),
  );
  const [last7Data] = createResource(
    () => ({ date: date(), loc: loc() }),
    ({ date, loc }) => fetchLast7(date, loc),
  );
  const [trendData] = createResource(
    () => {
      const d = new Date(date() + "T12:00:00Z");
      return { month: d.getUTCMonth() + 1, day: d.getUTCDate() };
    },
    ({ month, day }) => fetchAnnualTrend(month, day),
  );

  return (
    <div class="max-w-4xl mx-auto px-4 py-8 space-y-8">
      <h1 class="text-3xl font-bold">Je danes vroče v Sloveniji?</h1>

      {/* ── Controls ─────────────────────────────────────── */}
      <div class="flex flex-wrap gap-2 items-center text-sm">
        <button
          class="px-3 py-1.5 rounded-lg border border-[var(--color-rule)] hover:bg-[var(--color-paper-2)] disabled:opacity-30"
          disabled={date() <= "1950-01-01"}
          onClick={() => setDate(addDays(date(), -1))}
        >←</button>
        <input
          type="date" value={date()} max={today}
          class="px-3 py-1.5 rounded-lg border border-[var(--color-rule)] font-mono bg-[var(--color-card)]"
          onInput={(e) => setDate(e.currentTarget.value)}
        />
        <button
          class="px-3 py-1.5 rounded-lg border border-[var(--color-rule)] hover:bg-[var(--color-paper-2)] disabled:opacity-30"
          disabled={date() >= today}
          onClick={() => setDate(addDays(date(), 1))}
        >→</button>
        <select
          class="ml-2 px-3 py-1.5 rounded-lg border border-[var(--color-rule)] bg-[var(--color-card)]"
          onChange={(e) => setLoc(e.currentTarget.value || null)}
        >
          <option value="">Slovenija (vse postaje)</option>
          <For each={props.meta.stations}>
            {(s) => <option value={s.name}>{s.name.replace(/_/g, " ")}</option>}
          </For>
        </select>
      </div>

      {/* ── Card 1: Today + last-7 + distribution ────────── */}
      <Suspense fallback={<CardSkeleton h="h-64" />}>
        <Show when={todayData()} keyed>
          {(r) => (
            <div class="bg-[var(--color-card)] border border-[var(--color-rule)] rounded-2xl p-6 space-y-6">
              <TodayCard data={r} last7={last7Data()} meta={props.meta} />
              <Show when={r.available}>
                <div class="border-t border-dashed border-[var(--color-rule)] pt-4 space-y-2">
                  <div class="text-xs uppercase tracking-widest text-[var(--color-ink-soft)]">
                    Porazdelitev za ±7-dnevno okno
                  </div>
                  <Suspense fallback={<div class="h-48 animate-pulse bg-[var(--color-paper-2)] rounded-lg" />}>
                    <DistributionChart data={r} chartId="dist-chart" />
                  </Suspense>
                </div>
              </Show>
            </div>
          )}
        </Show>
      </Suspense>

      {/* ── Card 2: Annual national trend ────────────────── */}
      <Suspense fallback={<CardSkeleton h="h-72" />}>
        <Show when={trendData()} keyed>
          {(t) => (
            <div class="annual-trend-card bg-[var(--color-card)] border border-[var(--color-rule)] rounded-2xl p-6 space-y-3">
              <div class="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-1">
                <h2 class="font-bold text-lg">
                  Letni trend · okolica {t.dayLabel}
                </h2>
                <div class="annual-trend-stats text-xs text-[var(--color-ink-soft)]" />
              </div>
              <AnnualTrendChart data={t} chartId="annual-trend-chart" />
              <p class="text-xs text-[var(--color-ink-soft)]">
                90. percentil povprečne dnevne Tmax vseh postaj v ±30-dnevnem oknu ·
                Theil-Sen + Yue-Wang TFPW MK · projekcija do 2050
              </p>
            </div>
          )}
        </Show>
      </Suspense>
    </div>
  );
}

function Spinner() {
  return <div class="max-w-4xl mx-auto px-4 py-8 text-[var(--color-ink-soft)]">Nalaganje…</div>;
}

function CardSkeleton({ h }: { h: string }) {
  return <div class={`bg-[var(--color-card)] border border-[var(--color-rule)] rounded-2xl ${h} animate-pulse`} />;
}
