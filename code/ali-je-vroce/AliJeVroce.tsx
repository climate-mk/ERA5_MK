import { createSignal, createResource, Show, Suspense, For } from "solid-js";
import { fetchMeta, fetchTodayStatus, fetchLast7 } from "./api.ts";
import { TodayCard } from "./components/TodayCard.tsx";
import type { SiteMeta } from "../types/index.ts";

export function AliJeVroce() {
  const [meta] = createResource<SiteMeta>(fetchMeta);

  return (
    <Suspense fallback={<LoadingSkeleton />}>
      <Show when={meta()} fallback={<ErrorMsg />}>
        {(m) => <Dashboard meta={m()} />}
      </Show>
    </Suspense>
  );
}

function Dashboard(props: { meta: SiteMeta }) {
  const today = new Date().toISOString().slice(0, 10);
  const [date, setDate]   = createSignal(today);
  const [loc,  setLoc]    = createSignal<string | null>(null);

  const [todayData] = createResource(
    () => ({ date: date(), loc: loc() }),
    ({ date, loc }) => fetchTodayStatus(date, loc),
  );
  const [last7Data] = createResource(
    () => ({ date: date(), loc: loc() }),
    ({ date, loc }) => fetchLast7(date, loc),
  );

  const stations = () => props.meta.stations;

  return (
    <div class="max-w-4xl mx-auto px-4 py-8 space-y-8">
      <h1 class="text-3xl font-bold">Je danes vroče v Sloveniji?</h1>

      {/* Date + location controls */}
      <div class="flex flex-wrap gap-3 items-center">
        <button
          class="px-3 py-1.5 rounded-lg border border-[var(--color-rule)] text-sm hover:bg-[var(--color-paper-2)] disabled:opacity-30"
          disabled={date() <= "1950-01-01"}
          onClick={() => setDate(addDays(date(), -1))}
        >←</button>
        <input
          type="date"
          value={date()}
          max={today}
          class="px-3 py-1.5 rounded-lg border border-[var(--color-rule)] text-sm font-mono bg-[var(--color-card)]"
          onInput={(e) => setDate(e.currentTarget.value)}
        />
        <button
          class="px-3 py-1.5 rounded-lg border border-[var(--color-rule)] text-sm hover:bg-[var(--color-paper-2)] disabled:opacity-30"
          disabled={date() >= today}
          onClick={() => setDate(addDays(date(), 1))}
        >→</button>

        <select
          class="ml-2 px-3 py-1.5 rounded-lg border border-[var(--color-rule)] text-sm bg-[var(--color-card)]"
          onChange={(e) => setLoc(e.currentTarget.value || null)}
        >
          <option value="">Slovenija (vse postaje)</option>
          <For each={stations()}>
            {(s) => <option value={s.name}>{s.name.replace(/_/g, " ")}</option>}
          </For>
        </select>
      </div>

      {/* Today card */}
      <Suspense fallback={<CardSkeleton />}>
        <Show when={todayData()} keyed>
          {(r) => <TodayCard data={r} last7={last7Data()} meta={props.meta} />}
        </Show>
      </Suspense>
    </div>
  );
}

function addDays(dateStr: string, n: number): string {
  const d = new Date(dateStr + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function LoadingSkeleton() {
  return <div class="max-w-4xl mx-auto px-4 py-8 text-[var(--color-ink-soft)]">Nalaganje…</div>;
}

function ErrorMsg() {
  return <div class="max-w-4xl mx-auto px-4 py-8 text-red-600">Napaka pri nalaganju podatkov.</div>;
}

function CardSkeleton() {
  return (
    <div class="bg-[var(--color-card)] border border-[var(--color-rule)] rounded-2xl p-6 animate-pulse h-48" />
  );
}
