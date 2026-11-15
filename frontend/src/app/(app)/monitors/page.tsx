"use client";

import { useRouter } from "next/navigation";
import { Suspense, useState } from "react";
import { useProject, withProject } from "@/components/Project";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { time } from "@/lib/format";

interface Rule { id: string; project_id: string; name: string; metric: string; comparison: string; threshold: number; window_minutes: number; status: string; description: string }
interface Alert { id: string; rule: Rule; project: string; window_start: string; window_end: string; value: number; calls: number; acknowledged: boolean; investigation_run_id: string | null }

function MonitorsPage() {
  const { user } = useMe();
  const { project } = useProject();
  const router = useRouter();
  const toast = useToast();
  const rules = useApi<{ rules: Rule[] }>("/api/monitors/rules");
  const alerts = useApi<{ alerts: Alert[]; unacknowledged: number }>("/api/monitors/alerts", { refreshInterval: 30_000 });
  const [form, setForm] = useState({ name: "", metric: "error_rate", comparison: ">", threshold: "0.05", window: "15", tool: "", model: "" });
  const canEdit = user && user.role !== "viewer";
  const act = async (fn: () => Promise<unknown>, done: string) => { try { await fn(); toast(done); await Promise.all([rules.mutate(), alerts.mutate()]); } catch (e) { toast(e instanceof ApiError ? e.message : "That did not work.", "bad"); } };
  const investigate = async (a: Alert) => {
    try { const r = await api<{ id: string }>(`/api/monitors/alerts/${a.id}/investigate`, { method: "POST" }); router.push(withProject(`/investigations/?run=${r.id}`, project?.id)); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Could not start the investigation.", "bad"); }
  };
  const ruleList = rules.data?.rules ?? [];
  const active = ruleList.filter((r) => r.status === "active").length;
  // The most recent alert for each rule: the last time its window was found over the line.
  const lastAlert = (ruleId: string) => (alerts.data?.alerts ?? []).filter((a) => a.rule.id === ruleId).sort((x, y) => (x.window_end < y.window_end ? 1 : -1))[0];
  return (
    <Page title="Monitors" lede="Rules checked every five minutes against roll-ups. A window needs enough calls before it can raise an alert. Alerts appear here only."
      actions={canEdit && <button className="btn" onClick={() => act(async () => { const r = await api<{ raised: number }>("/api/monitors/evaluate", { method: "POST" }); toast(r.raised ? `${r.raised} new alert(s).` : "No new alerts."); }, "Rules checked.")}>Check rules now</button>}>
      <dl className="strip" aria-label="Monitor status">
        <div><dt>Rules active</dt><dd>{rules.data ? `${active} of ${ruleList.length}` : "…"}</dd></div>
        <div><dt>Rules paused</dt><dd>{rules.data ? ruleList.length - active : "…"}</dd></div>
        <div><dt>Alerts waiting for an answer</dt><dd>{alerts.data ? alerts.data.unacknowledged : "…"}</dd></div>
        <div><dt>Alerts on record</dt><dd>{alerts.data ? alerts.data.alerts.length : "…"}</dd></div>
      </dl>

      <section className="section" aria-labelledby="rules-h">
        <div className="section__head"><h2 id="rules-h">Rules</h2><span className="label">Each rule is one line: what it measures, the line, the window, and when it last fired.</span></div>
        <div className="table-wrap">
          {rules.error && <div className="p-4"><ErrorState error={rules.error} retry={() => rules.mutate()} /></div>}
          {rules.data?.rules.length === 0 && <Empty title="No monitors yet" />}
          {!!ruleList.length && (
            <table className="table">
              <thead><tr><th>Rule</th><th>Metric</th><th>Alert when</th><th>Window</th><th>Status</th><th>Last alert</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
              <tbody>{ruleList.map((r) => {
                const last = lastAlert(r.id);
                return (
                  <tr key={r.id}>
                    <td>{r.name}<div className="label">{r.description}</div></td>
                    <td className="measured">{r.metric}</td>
                    <td className="measured">{r.comparison} {r.threshold}</td>
                    <td className="measured">{r.window_minutes} min</td>
                    <td><span className="state" data-state={r.status}>{r.status}</span></td>
                    <td className="muted measured">{last ? time(last.window_end) : "none yet"}</td>
                    <td className="text-right">{canEdit && <button className="btn btn--sm" onClick={() => act(() => api(`/api/monitors/rules/${r.id}?status=${r.status === "active" ? "paused" : "active"}`, { method: "PATCH" }), "Updated.")}>{r.status === "active" ? "Pause" : "Resume"}</button>}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          )}
        </div>
      </section>

      <section className="section" aria-labelledby="alerts-h">
        <div className="section__head"><h2 id="alerts-h">Alert log</h2><span className="label">Newest first. Times are UTC windows.</span></div>
        <div className="table-wrap">
          {alerts.isLoading && <Loading />}
          {alerts.error && <div className="p-4"><ErrorState error={alerts.error} retry={() => alerts.mutate()} /></div>}
          {alerts.data?.alerts.length === 0 && <Empty title="No alerts have been raised" />}
          {!!alerts.data?.alerts.length && (
            <table className="table">
              <thead><tr><th>Window (UTC)</th><th>Rule</th><th>Project</th><th className="num">Measured</th><th className="num">Calls</th><th>State</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
              <tbody>{alerts.data.alerts.map((a) => (
                <tr key={a.id}>
                  <td className="log-time">{time(a.window_start)} to {new Date(a.window_end).toISOString().slice(11, 16)}</td>
                  <td>{a.rule.name}<div className="label">{a.rule.description}</div></td>
                  <td>{a.project}</td>
                  <td className="num bad">{a.value.toPrecision(4)}</td><td className="num">{a.calls}</td>
                  <td>{a.acknowledged ? <span className="state" data-state="paused">acknowledged</span> : <span className="state" data-state="active">open</span>}</td>
                  <td className="text-right"><div className="flex gap-1 justify-end">
                    {a.investigation_run_id ? <a className="btn btn--sm" href={withProject(`/investigations/?run=${a.investigation_run_id}`, project?.id)}>Open investigation</a>
                      : canEdit && <button className="btn btn--sm" onClick={() => investigate(a)}>Investigate</button>}
                    {!a.acknowledged && canEdit && <button className="btn btn--sm" onClick={() => act(() => api(`/api/monitors/alerts/${a.id}/ack`, { method: "POST" }), "Acknowledged.")}>Acknowledge</button>}
                  </div></td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </div>
      </section>

      {canEdit && project && (
        <form className="section" onSubmit={(e) => { e.preventDefault(); void act(() => api("/api/monitors/rules", { method: "POST", json: { project_id: project.id, name: form.name, metric: form.metric, comparison: form.comparison,
          threshold: Number(form.threshold), window_minutes: Number(form.window), filter: { ...(form.tool ? { tool: form.tool } : {}), ...(form.model ? { model: form.model } : {}) } } }), "Monitor created."); }}>
          <div className="section__head"><h2>New monitor for {project.name}</h2></div>
          <div className="form-grid items-end">
            <label className="field"><span className="label">Name</span><input className="input" required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
            <label className="field"><span className="label">Metric</span><select className="select" value={form.metric} onChange={(e) => setForm({ ...form, metric: e.target.value })}>
              <option value="error_rate">Error rate (0 to 1)</option><option value="p95_latency_ms">p95 latency (ms)</option><option value="cost_per_hour">Cost per hour (USD)</option></select></label>
            <label className="field"><span className="label">Alert when</span><select className="select" value={form.comparison} onChange={(e) => setForm({ ...form, comparison: e.target.value })}><option value=">">above</option><option value="<">below</option></select></label>
            <label className="field"><span className="label">Threshold</span><input className="input" type="number" step="any" required value={form.threshold} onChange={(e) => setForm({ ...form, threshold: e.target.value })} /></label>
            <label className="field"><span className="label">Over the last</span><select className="select" value={form.window} onChange={(e) => setForm({ ...form, window: e.target.value })}>
              {[5, 15, 30, 60, 180, 360, 1440].map((m) => (<option key={m} value={m}>{m < 60 ? `${m} minutes` : `${m / 60} hours`}</option>))}</select></label>
            <label className="field"><span className="label">Only this tool (optional)</span><input className="input" value={form.tool} onChange={(e) => setForm({ ...form, tool: e.target.value })} /></label>
            <label className="field"><span className="label">Only this model (optional)</span><input className="input" value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} /></label>
            <div className="actions"><button className="btn btn--primary">Create</button></div>
          </div>
        </form>
      )}
    </Page>
  );
}

export default function Monitors() {
  return (<Suspense fallback={<Loading />}><MonitorsPage /></Suspense>);
}
