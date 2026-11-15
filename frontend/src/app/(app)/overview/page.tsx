"use client";

import { Suspense } from "react";
import { Chart } from "@/components/Chart";
import { useProject } from "@/components/Project";
import { Page } from "@/components/Shell";
import { rangeOf, stepFor } from "@/components/TimeRange";
import { Empty, ErrorState, Loading, Meter, Spark } from "@/components/ui";
import { useApi } from "@/lib/api";
import { ms, rate, tokens, usd } from "@/lib/format";
import type { Figures } from "@/lib/types";
import { useQueryState } from "@/lib/url";

interface Overview { current: Figures; previous: Figures; model_calls: Figures }
interface Series { rows: (Figures & { t: string; model?: string; operation?: string; tool?: string })[] }

function change(now: number | null, before: number | null, kind: "ratio" | "points"): string {
  if (now == null || before == null) return "no earlier period";
  if (kind === "points") return `${now - before >= 0 ? "+" : ""}${((now - before) * 100).toFixed(1)} pts vs previous`;
  if (!before || before < now / 100) return "little data before";
  return `${now >= before ? "+" : ""}${(((now - before) / before) * 100).toFixed(1)}% vs previous`;
}

/** One gauge in the wall: a reading, its meter against the previous period, and a trace over time. */
function Gauge({ label, value, delta, worse, meter, spark, sparkLabel, children }: {
  label: string; value: string; delta: string; worse?: boolean;
  meter?: { now: number | null; before: number | null }; spark?: (number | null)[]; sparkLabel?: string; children?: React.ReactNode;
}) {
  return (
    <div className="stat">
      <span className="label">{label}</span>
      <span className="reading">{value}</span>
      {meter && <Meter now={meter.now} before={meter.before} />}
      {spark && sparkLabel && <Spark values={spark} label={sparkLabel} />}
      {children}
      <span className={`stat__delta measured ${worse === undefined ? "muted" : worse ? "bad" : "ok"}`}>{delta}</span>
    </div>
  );
}

function OverviewPage() {
  const { get } = useQueryState();
  const { project, isLoading: loadingProject } = useProject();
  const range = rangeOf(get("range"));
  const q = project ? `project=${project.id}&from=${encodeURIComponent(range.from)}&to=${encodeURIComponent(range.to)}` : null;
  const overview = useApi<Overview>(q && `/api/metrics/overview?${q}`);
  const byModel = useApi<Series>(q && `/api/metrics/series?${q}&step=${stepFor(range.hours)}&group_by=model&operation=chat`);
  const byTool = useApi<Series>(q && `/api/metrics/series?${q}&group_by=tool&operation=execute_tool`);
  const c = overview.data?.current, p = overview.data?.previous, m = overview.data?.model_calls;

  const times = [...new Set((byModel.data?.rows ?? []).map((r) => r.t))].sort();
  const callsOver = times.map((t) => (byModel.data?.rows ?? []).filter((r) => r.t === t).reduce((sum, r) => sum + (r.calls ?? 0), 0));
  const p95Over = times.map((t) => { const v = (byModel.data?.rows ?? []).filter((r) => r.t === t).map((r) => r.p95_ms).filter((x): x is number => x != null); return v.length ? Math.max(...v) : null; });

  const series = (rows: Series["rows"] = [], key: "p95_ms" | "calls") => {
    const labels = [...new Set(rows.map((r) => r.t))].sort();
    const names = [...new Set(rows.map((r) => r.model || "(none)"))];
    return { kind: "line" as const, title: "", x: "t", labels: labels.map((t) => new Date(t).toLocaleString(undefined, stepFor(range.hours) === "1d" ? { month: "short", day: "numeric" } : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })),
      series: names.map((n) => ({ name: n, data: labels.map((t) => rows.find((r) => r.t === t && (r.model || "(none)") === n)?.[key] ?? null) })) };
  };

  return (
    <Page title="Overview" lede={project ? `What ${project.name} did, from roll-ups updated as traces arrive.` : undefined}>
      {loadingProject && <div className="panel"><Loading /></div>}
      {!loadingProject && !project && <div className="panel"><Empty title="No projects yet">An administrator creates a project and an ingestion key under Administration, then an application sends traces to /v1/traces.</Empty></div>}
      {overview.error && <ErrorState error={overview.error} retry={() => overview.mutate()} />}
      {project && overview.isLoading && <div className="panel"><Loading rows={5} /></div>}
      {c && p && m && (c.calls === 0 ? (
        <div className="panel"><Empty title={`No traces from ${project?.name} in this range`}>Choose a longer range, or check that the application is sending to this project with its ingestion key.</Empty></div>
      ) : (
        <>
          <section className="stats" aria-label="Key figures">
            <Gauge label="Traces" value={tokens(c.roots)} delta={change(c.roots, p.roots, "ratio")} meter={{ now: c.roots, before: p.roots }} spark={callsOver} sparkLabel="Model calls over the range" />
            <Gauge label="Error rate, all spans" value={rate(c.error_rate)} delta={change(c.error_rate, p.error_rate, "points")} worse={c.error_rate != null && p.error_rate != null ? c.error_rate > p.error_rate : undefined}
              meter={{ now: c.error_rate, before: p.error_rate }} />
            <Gauge label="Model call p95" value={`up to ${ms(m.p95_ms)}`} delta={change(m.p95_ms, p.p95_ms, "ratio")} meter={{ now: m.p95_ms, before: p.p95_ms }} spark={p95Over} sparkLabel="Slowest model p95 per bucket" />
            <Gauge label="Model calls" value={tokens(m.calls)} delta={`${tokens(c.calls)} spans in total`} meter={{ now: m.calls, before: null }} spark={callsOver} sparkLabel="Model calls per bucket" />
            <Gauge label="Estimated cost" value={usd(c.cost_usd)} delta={`${tokens(c.input_tokens)} in, ${tokens(c.output_tokens)} out`} meter={{ now: c.cost_usd, before: null }} />
            <Gauge label="Tokens" value={tokens(c.input_tokens + c.output_tokens)} delta={`${tokens(c.input_tokens)} input, ${tokens(c.output_tokens)} output`} />
          </section>
          <div className="grid gap-4 lg:grid-cols-2 items-start">
            <section className="panel">
              <div className="panel__head"><h2>Model call latency, p95, by model</h2><span className="label">ms</span></div>
              <div className="panel__body">{byModel.data ? <Chart spec={series(byModel.data.rows, "p95_ms")} /> : <Loading />}</div>
            </section>
            <section className="panel">
              <div className="panel__head"><h2>Model calls by model</h2><span className="label">per bucket</span></div>
              <div className="panel__body">{byModel.data ? <Chart spec={series(byModel.data.rows, "calls")} /> : <Loading />}</div>
            </section>
          </div>
          <section className="panel table-wrap">
            <div className="panel__head"><h2>Tools</h2><span className="label">Latency percentiles are the upper bound of a histogram bucket.</span></div>
            {byTool.data && byTool.data.rows.length === 0 && <Empty title="No tool calls in this range" />}
            {byTool.data && byTool.data.rows.length > 0 && (
              <table className="table">
                <thead><tr><th>Tool</th><th className="num">Calls</th><th className="num">Error rate</th><th className="num">p50</th><th className="num">p95</th></tr></thead>
                <tbody>
                  {[...byTool.data.rows].sort((a, b) => (b.error_rate ?? 0) - (a.error_rate ?? 0)).map((r) => (
                    <tr key={r.tool}>
                      <td className="measured">{r.tool}</td><td className="num">{r.calls}</td>
                      <td className={`num ${(r.error_rate ?? 0) > 0.05 ? "bad" : ""}`}>{rate(r.error_rate)}</td><td className="num">{ms(r.p50_ms)}</td><td className="num">{ms(r.p95_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      ))}
    </Page>
  );
}

export default function OverviewRoute() {
  return (<Suspense fallback={<Loading />}><OverviewPage /></Suspense>);
}
