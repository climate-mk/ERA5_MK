import { onMount, onCleanup } from "solid-js";
import type { TodayStatus } from "../../types/index.ts";

interface Props {
  data:    TodayStatus;
  chartId: string;
}

const BAND_COLORS = [
  { from: -99, to: 10,  color: "rgba(58,90,138,0.12)"  },
  { from: 10,  to: 20,  color: "rgba(108,143,182,0.12)" },
  { from: 20,  to: 80,  color: "rgba(231,217,184,0.12)" },
  { from: 80,  to: 95,  color: "rgba(194,90,44,0.12)"  },
  { from: 95,  to: 101, color: "rgba(150,44,26,0.12)"  },
];

export function DistributionChart(props: Props) {
  let container!: HTMLDivElement;
  let chart: Highcharts.Chart | null = null;

  onMount(async () => {
    const Highcharts = (await import("highcharts")).default;
    const r = props.data;
    if (!r.available || !r.distribution?.length) return;

    const dist    = r.distribution;
    const cutoffs = r.cutoffs!;
    const todayX  = r.today_temp!;
    const color   = r.color!;

    // Convert percentile cutoffs to x-axis plotBands
    const pctToX = (pct: number): number => {
      const map: [number, number][] = [
        [0, cutoffs.p5 - (cutoffs.p10 - cutoffs.p5)],
        [5, cutoffs.p5], [10, cutoffs.p10], [20, cutoffs.p20],
        [50, cutoffs.p50], [80, cutoffs.p80], [95, cutoffs.p95],
        [100, cutoffs.p95 + (cutoffs.p95 - cutoffs.p80)],
      ];
      for (let i = 0; i < map.length - 1; i++) {
        const [pLo, xLo] = map[i]!;
        const [pHi, xHi] = map[i + 1]!;
        if (pct >= pLo && pct <= pHi) {
          return xLo + (xHi - xLo) * ((pct - pLo) / (pHi - pLo));
        }
      }
      return cutoffs.p95;
    };

    const plotBands = BAND_COLORS.map(({ from, to, color: bc }) => ({
      from: pctToX(from), to: pctToX(to), color: bc, zIndex: 0,
    }));

    chart = Highcharts.chart(container, {
      chart: {
        type: "area",
        animation: false,
        backgroundColor: "transparent",
        style: { fontFamily: "Space Grotesk, system-ui, sans-serif" },
      },
      title: { text: undefined },
      credits: { enabled: false },
      legend: { enabled: false },
      xAxis: {
        title: { text: "°C", style: { fontSize: "11px" } },
        plotLines: [{
          value:     todayX,
          color,
          width:     2,
          dashStyle: "Solid" as Highcharts.DashStyleValue,
          zIndex:    5,
          label: {
            text:  `${todayX.toFixed(1)} °C`,
            style: { color, fontWeight: "600", fontSize: "11px" },
            y:     14,
          },
        }],
        plotBands,
      },
      yAxis: {
        title: { text: undefined },
        labels: { enabled: false },
        gridLineWidth: 0,
      },
      tooltip: {
        formatter(this: Highcharts.Point) {
          if (typeof this.x === "number") {
            return `${(this.x as number).toFixed(1)} °C`;
          }
          return "";
        },
      },
      series: [{
        type:      "area",
        name:      "Porazdelitev",
        data:      dist,
        color,
        fillOpacity: 0.18,
        lineWidth:   2,
        marker:    { enabled: false },
      }],
    } as Highcharts.Options);
  });

  onCleanup(() => { chart?.destroy(); chart = null; });

  return <div id={props.chartId} ref={container} style={{ "min-height": "200px" }} />;
}
