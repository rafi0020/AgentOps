"use client";

import { useRouter } from "next/navigation";
import { Suspense, useState } from "react";
import { useProject, withProject } from "@/components/Project";
import { Page } from "@/components/Shell";
import { rangeOf } from "@/components/TimeRange";
import { TraceTimeline } from "@/components/TraceTimeline";
import { Empty, ErrorState, Loading, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { ms, time, tokens, usd } from "@/lib/format";
import type { SpanRow, TraceRow } from "@/lib/types";
import { useQueryState } from "@/lib/url";

const FILTERS = ["status", "model", "tool", "service_version", "session", "q"] as const;

function TraceList() {
  const { get, set, params } = useQueryState();
  const { project } = useProject();
  const range = rangeOf(get("range"));
  const [extra, setExtra] = useState<{ traces: TraceRow[]; next: string | null; key: string }>({ traces: [], next: null, key: "" });
  const query = new URLSearchParams({ project: project?.id ?? "", from: range.from, to: range.to, limit: "50" });
  FILTERS.forEach((f) => { const v = params.get(f); if (v) query.set(f, v); });
  const first = useApi<{ traces: TraceRow[]; next_cursor: string | null }>(project ? `/api/traces?${query}` : null);
  const sameView = extra.key === query.toString();  // pages loaded for another filter are dropped
  const traces = [...(first.data?.traces ?? []), ...(sameView ? extra.traces : [])];
  const next = sameView && extra.traces.length ? extra.next : first.data?.next_cursor;
  const loadMore = async () => {
    if (!next) return;
    const page = await api<{ traces: TraceRow[]; next_cursor: string | null }>(`/api/traces?${query}&cursor=${encodeURIComponent(next)}`);
    setExtra({ traces: [...(sameView ? extra.traces : []), ...page.traces], next: page.next_cursor, key: query.toString() });
  };
  return (
    <Page title="Traces" lede="One row per request your application handled. Filters stay in the address, so a view can be shared.">
      <form className="toolbar" onSubmit={(e) => { e.preventDefault(); const f = new FormData(e.currentTarget); set(Object.fromEntries(FILTERS.map((k) => [k, String(f.get(k) ?? "")]))); }}>
        <label className="field"><span className="label">Status</span>
          <select className="select" name="status" defaultValue={get("status") ?? ""}><option value="">Any</option><option value="error">Failed</option><option value="ok">Succeeded</option></select></label>
        <label className="field"><span className="label">Model</span><input className="input measured" name="model" defaultValue={get("model") ?? ""} /></label>
        <label className="field"><span className="label">Tool</span><input className="input measured" name="tool" defaultValue={get("tool") ?? ""} /></label>
        <label className="field"><span className="label">Version</span><input className="input measured" name="service_version" defaultValue={get("service_version") ?? ""} /></label>
        <label className="field"><span className="label">Name contains</span><input className="input" name="q" defaultValue={get("q") ?? ""} /></label>
        <button className="btn btn--primary">Apply</button>
      </form>
      <div className="panel table-wrap">
        {first.isLoading && <Loading rows={8} />}
        {first.error && <div className="p-4"><ErrorState error={first.error} retry={() => first.mutate()} /></div>}
        {first.data && traces.length === 0 && <Empty title="No traces match">Widen the time range or clear a filter.</Empty>}
        {traces.length > 0 && (
          <table className="table">
            <thead><tr><th>Trace</th><th>Started</th><th>Status</th><th className="num">Duration</th><th className="num">Spans</th><th className="num">Tokens</th><th className="num">Est. cost</th><th>Models</th></tr></thead>
            <tbody>
              {traces.map((t) => (
                <tr key={t.trace_id}>
                  <td><a href={withProject(`/traces/?trace=${t.trace_id}`, project?.id)}>{t.root_name}</a></td>
                  <td className="muted measured">{time(t.start_time)}</td>
                  <td className={t.status === "error" ? "bad" : "muted"}>{t.status === "error" ? `failed (${t.error_count})` : "ok"}</td>
                  <td className="num">{ms(t.duration_ms)}</td><td className="num">{t.span_count}</td><td className="num">{tokens(t.tokens)}</td>
                  <td className="num">{usd(t.cost_usd)}</td><td className="muted measured">{t.models.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {next && <div><button className="btn" onClick={loadMore}>Load more</button></div>}
    </Page>
  );
}

function TraceDetail({ id }: { id: string }) {
  const { user } = useMe();
  const { project } = useProject();
  const router = useRouter();
  const toast = useToast();
  const { data, error } = useApi<TraceRow & { spans: SpanRow[]; truncated: boolean }>(project ? `/api/traces/${project.id}/${id}` : null);
  const canAct = user && user.role !== "viewer";
  const investigate = async () => {
    try {
      const r = await api<{ id: string }>("/api/investigations", { method: "POST", json: { question: "Why did this trace behave the way it did?", project_id: project?.id, trace_id: id } });
      router.push(withProject(`/investigations/?run=${r.id}`, project?.id));
    } catch (e) { toast(e instanceof ApiError ? e.message : "Could not start the investigation.", "bad"); }
  };
  if (error) return <Page title="Trace"><ErrorState error={error} /></Page>;
  if (!data) return <Page title="Trace"><Loading rows={8} /></Page>;
  return (
    <Page title={data.root_name} lede={`${data.service_name ?? ""} ${data.service_version ?? ""}, ${time(data.start_time)}, trace ${id}`}
      actions={<>{canAct && <button className="btn btn--primary" onClick={investigate}>Investigate this trace</button>}<a className="btn" href={withProject("/traces/", project?.id)}>All traces</a></>}>
      <section className="stats" aria-label="Trace figures">
        <div className="stat"><span className="label">Status</span><span className={`reading ${data.status === "error" ? "bad" : ""}`}>{data.status === "error" ? "failed" : "ok"}</span></div>
        <div className="stat"><span className="label">Duration</span><span className="reading">{ms(data.duration_ms)}</span></div>
        <div className="stat"><span className="label">Tokens</span><span className="reading">{tokens(data.tokens)}</span></div>
        <div className="stat"><span className="label">Estimated cost</span><span className="reading">{usd(data.cost_usd)}</span></div>
      </section>
      {data.truncated && <div className="banner">This trace has more than 2,000 spans; the first 2,000 are shown.</div>}
      <TraceTimeline spans={data.spans} total={data.duration_ms} canSeeContent={user?.role !== "viewer"} />
    </Page>
  );
}

function TracesRoute() {
  const { get } = useQueryState();
  const trace = get("trace");
  return trace ? <TraceDetail key={trace} id={trace} /> : <TraceList />;
}

export default function Traces() {
  return (<Suspense fallback={<Loading />}><TracesRoute /></Suspense>);
}
