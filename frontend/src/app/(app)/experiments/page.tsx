"use client";

import { Suspense, useState } from "react";
import { useProject } from "@/components/Project";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { ms, rate, time, usd } from "@/lib/format";
import type { Evaluator, ExperimentRow, Score } from "@/lib/types";
import { useQueryState } from "@/lib/url";

const show = (v: unknown) => (v == null ? "" : typeof v === "string" ? v : JSON.stringify(v));
type Result = { id: string; item_id: string; input: unknown; expected: unknown; output: unknown; error: string | null; latency_ms: number | null; scores: Record<string, Score> };
type Totals = Record<string, { n: number; passed: number; invalid: number; mean: number | null; pass_rate: number | null }>;
interface Comparison {
  a: ExperimentRow; b: ExperimentRow;
  per_evaluator: Record<string, { mean_a: number | null; mean_b: number | null; delta: number | null }>;
  regressions: number; improvements: number;
  items: { item_id: string; change: string; scores_a: Record<string, Score>; scores_b: Record<string, Score> }[];
}

const signed = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(3)}`;

function ScoreCell({ s, evaluator, canLabel, resultId }: { s?: Score; evaluator?: Evaluator; canLabel: boolean; resultId: string }) {
  const toast = useToast();
  if (!s) return <td />;
  const label = async (score: number) => {
    try { await api("/api/labels", { method: "POST", json: { evaluator_id: evaluator?.id, result_id: resultId, score } }); toast("Your grade was recorded."); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Could not record the grade.", "bad"); }
  };
  return (
    <td>
      <span className={s.invalid ? "muted" : s.passed ? "ok" : "bad"}>{s.invalid ? "no score" : s.raw != null ? `${s.raw}/5` : s.passed ? "pass" : "fail"}</span>
      <div className="label">{s.reason}</div>
      {canLabel && evaluator?.kind === "judge" && !s.invalid && (
        <div className="flex gap-1 mt-1" aria-label="Your own grade">{[1, 2, 3, 4, 5].map((n) => (<button key={n} className="btn btn--sm" onClick={() => label(n)}>{n}</button>))}</div>
      )}
    </td>
  );
}

/** One run as a single column: its figure per evaluator, then every item it produced. */
function RunView({ data, evaluators }: { data: ExperimentRow & { results: Result[] }; evaluators: Evaluator[] }) {
  const { user } = useMe();
  const used = evaluators.filter((e) => data.evaluator_ids.includes(e.id));
  const totals: Totals = data.totals ?? {};
  return (
    <>
      {data.error && <div className="banner banner--bad">{data.error}</div>}
      <div className="table-wrap">
        <table className="table matrix">
          <thead><tr><th>Measure</th><th className="num">{data.name}</th></tr></thead>
          <tbody>
            {used.map((e) => {
              const t = totals[e.id];
              return (
                <tr key={e.id}>
                  <td>{e.name}{e.calibration && e.calibration.status !== "calibrated" ? <div className="label">judge not calibrated</div> : null}</td>
                  <td className="num">{t?.mean == null ? (t ? "no score" : "running") : t.mean.toFixed(3)}
                    {t && <div className="label">{rate(t.pass_rate)} passed{t.invalid ? `, ${t.invalid} unscored` : ""}</div>}</td>
                </tr>
              );
            })}
            <tr><td>Items done</td><td className="num">{data.items_done} of {data.items_total}</td></tr>
            <tr><td>Estimated cost</td><td className="num">{usd(data.cost_usd)}</td></tr>
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Input</th><th>Expected</th><th>Output</th><th className="num">Latency</th>{used.map((e) => (<th key={e.id}>{e.name}</th>))}</tr></thead>
          <tbody>{data.results.map((r) => (
            <tr key={r.id}>
              <td className="ink-2 measured">{show(r.input)}</td><td className="ink-2 measured">{show(r.expected)}</td>
              <td className={r.error ? "bad" : "ink-2 measured"}>{r.error ?? show(r.output)}</td><td className="num">{ms(r.latency_ms)}</td>
              {used.map((e) => (<ScoreCell key={e.id} s={r.scores[e.id]} evaluator={e} resultId={r.id} canLabel={user?.role !== "viewer"} />))}
            </tr>
          ))}</tbody>
        </table>
      </div>
    </>
  );
}

/** Two runs as columns, the first as the baseline; each change is a signed figure, coloured only for regressions and gains. */
function Matrix({ data, evaluators }: { data: Comparison; evaluators: Evaluator[] }) {
  const name = (id: string) => evaluators.find((e) => e.id === id)?.name ?? id.slice(0, 8);
  const ids = Object.keys(data.per_evaluator);
  const items = data.items.filter((i) => i.change !== "same").slice(0, 200);
  return (
    <>
      <div className="table-wrap">
        <table className="table matrix">
          <thead><tr><th>Measure</th><th className="num">{data.a.name} (baseline)</th><th className="num">{data.b.name}</th><th className="num">Change</th></tr></thead>
          <tbody>
            {ids.map((id) => {
              const v = data.per_evaluator[id];
              const sign = v.delta == null || v.delta === 0 ? undefined : v.delta < 0 ? "worse" : "better";
              return (
                <tr key={id}>
                  <td>{name(id)}</td>
                  <td className="num">{v.mean_a?.toFixed(3) ?? ""}</td>
                  <td className="num">{v.mean_b?.toFixed(3) ?? ""}</td>
                  <td className="num"><span className="delta" data-sign={sign}>{v.delta == null ? "" : signed(v.delta)}</span></td>
                </tr>
              );
            })}
            <tr><th scope="row">Items that got worse</th><td /><td /><td className="num"><span className="delta" data-sign={data.regressions ? "worse" : undefined}>{data.regressions}</span></td></tr>
            <tr><th scope="row">Items that got better</th><td /><td /><td className="num"><span className="delta" data-sign={data.improvements ? "better" : undefined}>{data.improvements}</span></td></tr>
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Item</th><th>Change</th>{ids.map((id) => (<th key={id}>{name(id)}: before, after</th>))}</tr></thead>
          <tbody>{items.length === 0 && <tr><td colSpan={2 + ids.length} className="muted">No item changed between these runs.</td></tr>}
            {items.map((i) => (
              <tr key={i.item_id}>
                <td className="measured muted">{i.item_id.slice(0, 8)}</td>
                <td className={i.change === "regression" ? "bad" : "ok"}>{i.change === "regression" ? "worse" : "better"}</td>
                {ids.map((id) => (<td key={id} className="ink-2 measured">{i.scores_a[id]?.passed ? "pass" : "fail"}, {i.scores_b[id]?.passed ? "pass" : "fail"}</td>))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function NewExperiment({ projectId, evaluators, onDone }: { projectId: string; evaluators: Evaluator[]; onDone: (id: string) => void }) {
  const toast = useToast();
  const datasets = useApi<{ datasets: { id: string; name: string; items: number }[] }>(`/api/datasets?project=${projectId}`);
  const prompts = useApi<{ prompts: { id: string; name: string; latest_version: number | null }[] }>(`/api/prompts?project=${projectId}`);
  const [form, setForm] = useState({ name: "", dataset: "", kind: "prompt", prompt: "", version: "1", url: "", output_path: "", evaluators: [] as string[] });
  const submit = async () => {
    const target = form.kind === "prompt" ? { kind: "prompt", prompt_id: form.prompt, version: Number(form.version) } : { kind: "http", url: form.url, output_path: form.output_path };
    try { const r = await api<{ id: string }>("/api/experiments", { method: "POST", json: { project_id: projectId, dataset_id: form.dataset, name: form.name, target, evaluator_ids: form.evaluators } }); onDone(r.id); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Could not start the experiment.", "bad"); }
  };
  return (
    <form className="panel" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <div className="panel__head"><h2>Start a run</h2><span className="label">Every output is scored by the evaluators you tick.</span></div>
      <div className="panel__body form-grid">
        <label className="field"><span className="label">Name</span><input className="input" required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
        <label className="field"><span className="label">Dataset</span><select className="select" required value={form.dataset} onChange={(e) => setForm({ ...form, dataset: e.target.value })}>
          <option value="">Choose…</option>{datasets.data?.datasets.map((d) => (<option key={d.id} value={d.id}>{d.name} ({d.items})</option>))}</select></label>
        <label className="field"><span className="label">Run against</span><select className="select" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
          <option value="prompt">A prompt version</option><option value="http">My application (HTTP)</option></select></label>
        {form.kind === "prompt" ? (<>
          <label className="field"><span className="label">Prompt</span><select className="select" required value={form.prompt} onChange={(e) => setForm({ ...form, prompt: e.target.value })}>
            <option value="">Choose…</option>{prompts.data?.prompts.map((p) => (<option key={p.id} value={p.id}>{p.name}</option>))}</select></label>
          <label className="field"><span className="label">Version</span><input className="input" type="number" min={1} value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })} /></label>
        </>) : (<>
          <label className="field"><span className="label">URL</span><input className="input measured" required placeholder="https://example.com/answer" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} /></label>
          <label className="field"><span className="label">Answer is at (JSON path)</span><input className="input measured" placeholder="answer" value={form.output_path} onChange={(e) => setForm({ ...form, output_path: e.target.value })} /></label>
        </>)}
        <fieldset className="span-2 flex flex-wrap gap-3 border-0 p-0 m-0">
          <legend className="label mb-1">Evaluators</legend>
          {evaluators.map((e) => (<label key={e.id} className="flex items-center gap-2"><input type="checkbox" checked={form.evaluators.includes(e.id)}
            onChange={(x) => setForm({ ...form, evaluators: x.target.checked ? [...form.evaluators, e.id] : form.evaluators.filter((i) => i !== e.id) })} />{e.name} <span className="muted">({e.kind})</span></label>))}
          {evaluators.length === 0 && <span className="muted">Create an evaluator first (below).</span>}
        </fieldset>
        <div className="span-2 actions"><button className="btn btn--primary" disabled={!form.evaluators.length || !form.dataset}>Start</button></div>
      </div>
    </form>
  );
}

function NewEvaluator({ projectId, onDone }: { projectId: string; onDone: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState({ name: "", kind: "contains", text: "", pattern: "", rubric: "", ms: "1000" });
  const submit = async () => {
    const config = { contains: { text: form.text }, regex: { pattern: form.pattern }, judge: { rubric: form.rubric }, latency_under: { ms: Number(form.ms) }, exact: {}, numeric: { tolerance: 0.01 } }[form.kind] ?? {};
    try { await api("/api/evaluators", { method: "POST", json: { project_id: projectId, name: form.name, kind: form.kind, config } }); toast("Evaluator created."); onDone(); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Could not create the evaluator.", "bad"); }
  };
  return (
    <form className="panel" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <div className="panel__head"><h2>New evaluator</h2></div>
      <div className="panel__body form-grid items-end">
        <label className="field"><span className="label">Name</span><input className="input" required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
        <label className="field"><span className="label">Kind</span><select className="select" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
          <option value="contains">Output contains text</option><option value="exact">Matches expected exactly</option><option value="numeric">Number matches expected</option>
          <option value="regex">Matches a pattern</option><option value="latency_under">Faster than</option><option value="judge">LLM judge with a rubric</option></select></label>
        {form.kind === "contains" && <label className="field"><span className="label">Text (blank: the item&apos;s expected output)</span><input className="input" value={form.text} onChange={(e) => setForm({ ...form, text: e.target.value })} /></label>}
        {form.kind === "regex" && <label className="field"><span className="label">Pattern</span><input className="input measured" required value={form.pattern} onChange={(e) => setForm({ ...form, pattern: e.target.value })} /></label>}
        {form.kind === "latency_under" && <label className="field"><span className="label">Milliseconds</span><input className="input" type="number" value={form.ms} onChange={(e) => setForm({ ...form, ms: e.target.value })} /></label>}
        {form.kind === "judge" && <label className="field span-2"><span className="label">Rubric: what does a good answer do?</span><textarea className="textarea" rows={2} required value={form.rubric} onChange={(e) => setForm({ ...form, rubric: e.target.value })} />
          <span className="hint">A judge&apos;s scores count for releases only after 20 of its grades have been checked against your own.</span></label>}
        <div className="span-2 actions"><button className="btn btn--primary">Create</button></div>
      </div>
    </form>
  );
}

function ExperimentsPage() {
  const { get, set } = useQueryState();
  const { user } = useMe();
  const { project } = useProject();
  const [picked, setPicked] = useState<string[]>([]);
  const experimentId = get("experiment");
  const a = get("a"), b = get("b");
  const compareMode = !!(a && b);
  const list = useApi<{ experiments: ExperimentRow[] }>(project ? `/api/experiments?project=${project.id}` : null, { refreshInterval: 5000 });
  const evs = useApi<{ evaluators: Evaluator[] }>(project ? `/api/evaluators?project=${project.id}` : null);
  const run = useApi<ExperimentRow & { results: Result[] }>(experimentId ? `/api/experiments/${experimentId}` : null, { refreshInterval: (d) => (d && ["queued", "running"].includes(d.status) ? 2000 : 0) });
  const cmp = useApi<Comparison>(compareMode ? `/api/experiments/compare?a=${a}&b=${b}` : null);
  const evaluators = evs.data?.evaluators ?? [];
  const canEdit = user && user.role !== "viewer";
  const selected = !!experimentId || compareMode;
  const reset = () => { setPicked([]); set({ experiment: null, a: null, b: null }); };

  const title = compareMode ? (cmp.data ? `${cmp.data.a.name} against ${cmp.data.b.name}` : "Comparison")
    : experimentId ? (run.data?.name ?? "Run") : "Experiments";
  const lede = compareMode
    ? cmp.data ? `${cmp.data.regressions} ${cmp.data.regressions === 1 ? "item" : "items"} got worse and ${cmp.data.improvements} got better. Items that changed are listed first.` : undefined
    : experimentId ? (run.data ? `${run.data.items_done} of ${run.data.items_total} items, estimated cost ${usd(run.data.cost_usd)}. Started ${time(run.data.created_at)}.` : undefined)
    : "Run a dataset through a prompt version or your application, score every output, and compare two runs.";
  const actions = (
    <>
      {experimentId && run.data && <StatusChip status={run.data.status} />}
      {selected && <button className="btn" onClick={reset}>All experiments</button>}
      {!selected && picked.length === 2 && <button className="btn btn--primary" onClick={() => set({ a: picked[0], b: picked[1] })}>Compare the two selected</button>}
    </>
  );

  return (
    <Page title={title} lede={lede} actions={actions}>
      <div className="split" data-selected={selected}>
        <section className="split__list" aria-label="Runs">
          <div className="split__bar">
            <h2>Runs</h2>
            <button className="btn btn--sm" onClick={reset}>New run</button>
          </div>
          {list.isLoading && <Loading />}
          {list.error && <div className="p-4"><ErrorState error={list.error} retry={() => list.mutate()} /></div>}
          {list.data?.experiments.length === 0 && <Empty title="No experiments in this project yet">Start one from the main area.</Empty>}
          {!!list.data?.experiments.length && (
            <table className="picklist">
              <thead><tr><th><span className="visually-hidden">Select</span></th><th>Run</th><th className="num">Items</th></tr></thead>
              <tbody>{list.data.experiments.map((e) => {
                const current = e.id === experimentId || (compareMode && (e.id === a || e.id === b));
                return (
                  <tr key={e.id} data-selected={current}>
                    <td><input type="checkbox" aria-label={`Select ${e.name}`} checked={picked.includes(e.id)} onChange={(x) => setPicked(x.target.checked ? [...picked.slice(-1), e.id] : picked.filter((p) => p !== e.id))} /></td>
                    <td>
                      <button className="pick" aria-current={e.id === experimentId ? "true" : undefined} onClick={() => set({ experiment: e.id, a: null, b: null })}>{e.name}</button>
                      <div className="label measured">{e.target.kind === "prompt" ? `prompt v${String(e.target.version)}` : String(e.target.url)}</div>
                      <div className="label"><StatusChip status={e.status} /> {time(e.created_at)}</div>
                    </td>
                    <td className="num">{e.items_done}/{e.items_total}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          )}
          <div className="split__bar split__bar--ruled"><h2>Evaluators</h2></div>
          {evaluators.length === 0 ? <Empty title="No evaluators yet" /> : (
            <table className="picklist">
              <thead><tr><th>Evaluator</th><th>Calibration</th></tr></thead>
              <tbody>{evaluators.map((e) => (
                <tr key={e.id}><td>{e.name}<div className="label">{e.kind}</div></td>
                  <td className={e.calibration ? (e.calibration.status === "calibrated" ? "ok" : "bad") : "muted"}>{e.calibration ? e.calibration.why : "not a judge"}</td></tr>
              ))}</tbody>
            </table>
          )}
        </section>

        <section className="split__pane" aria-label={compareMode ? "Comparison" : experimentId ? "Run" : "Start a run"}>
          {!selected && (
            <div className="split__body">
              {!!list.data?.experiments.length && <p className="legend-line">Tick two runs on the left to compare them, or open one to see its scores.</p>}
              {canEdit && project && <NewExperiment projectId={project.id} evaluators={evaluators} onDone={(id) => set({ experiment: id })} />}
              {canEdit && project && <NewEvaluator projectId={project.id} onDone={() => evs.mutate()} />}
            </div>
          )}
          {experimentId && (
            <div className="split__body">
              {run.error && <ErrorState error={run.error} retry={() => run.mutate()} />}
              {!run.data && !run.error && <Loading rows={6} />}
              {run.data && <RunView data={run.data} evaluators={evaluators} />}
            </div>
          )}
          {compareMode && (
            <div className="split__body">
              {cmp.error && <ErrorState error={cmp.error} retry={() => cmp.mutate()} />}
              {!cmp.data && !cmp.error && <Loading rows={6} />}
              {cmp.data && <Matrix data={cmp.data} evaluators={evaluators} />}
            </div>
          )}
        </section>
      </div>
    </Page>
  );
}

export default function Experiments() {
  return (<Suspense fallback={<Loading />}><ExperimentsPage /></Suspense>);
}
