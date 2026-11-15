"use client";

import { Fragment, Suspense, useState } from "react";
import { useProject } from "@/components/Project";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { diffLines } from "@/lib/diff";
import { time } from "@/lib/format";
import { GateChecks } from "@/components/GateChecks";
import type { Evaluator, ExperimentRow, Release } from "@/lib/types";
import { useQueryState } from "@/lib/url";

interface Version { version: number; text: string; variables: string[]; note: string | null; created_at: string }
interface Detail { id: string; name: string; production_version: number | null; gate: { primary_evaluator_id: string | null; min_score: number | null; max_drop: number }; versions: Version[]; releases: Release[] }
interface PromptRow { id: string; name: string; production_version: number | null; latest_version: number | null }

/** The first non-empty line of a version, as its subject line in the log. */
const subject = (text: string) => text.split("\n").find((l) => l.trim())?.trim() ?? "(empty)";

/** The most recent release that asked for a version: its state is the version's state until it is decided. */
function releaseOf(releases: Release[], version: number): Release | undefined {
  return releases.filter((r) => r.to_version === version).sort((x, y) => (x.created_at < y.created_at ? 1 : -1))[0];
}

function PromptView({ id, onClose }: { id: string; onClose: () => void }) {
  const { user } = useMe();
  const toast = useToast();
  const { project } = useProject();
  const { data, error, mutate } = useApi<Detail>(`/api/prompts/id/${id}`);
  const experiments = useApi<{ experiments: ExperimentRow[] }>(project ? `/api/experiments?project=${project.id}` : null);
  const evaluators = useApi<{ evaluators: Evaluator[] }>(project ? `/api/evaluators?project=${project.id}` : null);
  const [text, setText] = useState("");
  const [compare, setCompare] = useState<[number, number] | null>(null);
  const [request, setRequest] = useState({ version: "", experiment: "" });
  if (error) return <div className="split__body"><ErrorState error={error} retry={() => mutate()} /></div>;
  if (!data) return <div className="split__body"><Loading rows={6} /></div>;
  const canEdit = user && user.role !== "viewer";
  const act = async (fn: () => Promise<unknown>, done: string) => { try { await fn(); toast(done); await mutate(); } catch (e) { toast(e instanceof ApiError ? e.message : "That did not work.", "bad"); await mutate(); } };
  const [older, newer] = compare ?? [data.versions[1]?.version, data.versions[0]?.version];
  const a = data.versions.find((v) => v.version === older), b = data.versions.find((v) => v.version === newer);
  const relevant = (experiments.data?.experiments ?? []).filter((e) => e.target.kind === "prompt" && e.target.prompt_id === id && String(e.target.version) === request.version);
  const lines = a && b ? diffLines(a.text, b.text) : [];
  return (
    <>
      <div className="split__bar">
        <div className="min-w-0">
          <button className="btn btn--sm split__back" onClick={onClose}>All prompts</button>
          <h2>{data.name}</h2>
        </div>
        <span className="label">{data.production_version ? `Version ${data.production_version} is live. Applications read it with a key that has the prompts:read scope.` : "No version is live yet."}</span>
      </div>
      <div className="split__body">
        <section className="section" aria-labelledby="versions-h">
          <div className="section__head"><h2 id="versions-h">Version log</h2><span className="label">A change is a new version; versions are never edited.</span></div>
          {data.versions.length === 0 && <Empty title="No versions yet" />}
          <ol className="gitlog" aria-label="Versions, newest first">
            {data.versions.map((v) => {
              const rel = releaseOf(data.releases, v.version);
              const live = v.version === data.production_version;
              const state = live ? "live" : rel ? rel.status.replace(/_/g, " ") : "";
              const tone = live || rel?.status === "approved" ? "ok" : rel?.status === "rejected" || rel?.status === "blocked" ? "bad" : "";
              return (
                <li key={v.version} data-live={live}>
                  <span className="gitlog__ver">v{v.version}</span>
                  <span className="gitlog__subject" title={v.text}>{subject(v.text)}</span>
                  <span className={`gitlog__meta gitlog__state ${tone ? `gitlog__state--${tone}` : ""}`}>{state}</span>
                  <span className="gitlog__when">{time(v.created_at)}{v.variables.length ? ` · variables: ${v.variables.join(", ")}` : ""}</span>
                  {v.note && <span className="gitlog__note">{v.note}</span>}
                </li>
              );
            })}
          </ol>
          {canEdit && (
            <form className="section__form" onSubmit={(e) => { e.preventDefault(); void act(() => api(`/api/prompts/id/${id}/versions`, { method: "POST", json: { text } }), "New version saved."); setText(""); }}>
              <label className="field wide"><span className="label">New version (use {"{{name}}"} for variables)</span><textarea className="textarea measured" rows={4} value={text} onChange={(e) => setText(e.target.value)} /></label>
              <button className="btn" disabled={!text.trim()}>Save as a new version</button>
            </form>
          )}
        </section>

        {a && b && (
          <section className="section" aria-labelledby="diff-h">
            <div className="section__head">
              <h2 id="diff-h">Difference</h2>
              <span className="flex gap-2 items-center">
                <label className="flex gap-2 items-center label">Older
                  <select className="select" value={older} onChange={(e) => setCompare([Number(e.target.value), newer])}>{data.versions.map((v) => (<option key={v.version} value={v.version}>v{v.version}</option>))}</select></label>
                <label className="flex gap-2 items-center label">Newer
                  <select className="select" value={newer} onChange={(e) => setCompare([older, Number(e.target.value)])}>{data.versions.map((v) => (<option key={v.version} value={v.version}>v{v.version}</option>))}</select></label>
              </span>
            </div>
            <div className="dlines" role="group" aria-label={`Changes from v${a.version} to v${b.version}`}>
              {lines.map((p, i) => (
                <div key={i} className="dline" data-kind={p.kind === "same" ? undefined : p.kind === "add" ? "add" : "remove"}>
                  <span className="dline__sign" aria-hidden>{p.kind === "add" ? "+" : p.kind === "remove" ? "-" : " "}</span>
                  <span className="dline__text"><span className="visually-hidden">{p.kind === "add" ? "added: " : p.kind === "remove" ? "removed: " : ""}</span>{p.text || " "}</span>
                </div>
              ))}
            </div>
          </section>
        )}

        {canEdit && (
          <form className="section" onSubmit={(e) => { e.preventDefault(); void act(() => api(`/api/prompts/id/${id}/releases`, { method: "POST", json: { to_version: Number(request.version), experiment_id: request.experiment || null } }), "Release requested. See the gate below."); }}>
            <div className="section__head"><h2>Request a release</h2><span className="label">The gate checks an experiment that tested this version.</span></div>
            <div className="form-grid items-end">
              <label className="field"><span className="label">Version</span><select className="select" required value={request.version} onChange={(e) => setRequest({ version: e.target.value, experiment: "" })}>
                <option value="">Choose…</option>{data.versions.map((v) => (<option key={v.version} value={v.version}>v{v.version}</option>))}</select></label>
              <label className="field"><span className="label">Experiment that tested it</span><select className="select" value={request.experiment} onChange={(e) => setRequest({ ...request, experiment: e.target.value })}>
                <option value="">None</option>{relevant.map((x) => (<option key={x.id} value={x.id}>{x.name} ({x.status})</option>))}</select></label>
              <div className="actions"><button className="btn btn--primary" disabled={!request.version}>Request</button></div>
            </div>
          </form>
        )}
        {user?.role === "admin" && <GateSettings id={id} gate={data.gate} evaluators={evaluators.data?.evaluators ?? []} onSaved={() => mutate()} />}

        <section className="section" aria-labelledby="history-h">
          <div className="section__head"><h2 id="history-h">Release history</h2></div>
          {data.releases.length === 0 && <Empty title="No releases yet" />}
          {data.releases.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Release</th><th>State</th><th>Requested by</th><th>When</th></tr></thead>
                <tbody>{data.releases.map((r) => (
                  <Fragment key={r.id}>
                    <tr>
                      <td>{r.from_version == null ? `v${r.to_version} (first release)` : `v${r.from_version} to v${r.to_version}`}</td>
                      <td><StatusChip status={r.status} /></td>
                      <td className="muted">{r.requested_by ?? ""}</td>
                      <td className="muted measured">{time(r.created_at)}</td>
                    </tr>
                    <tr className="detail">
                      <td colSpan={4}><div className="grid gap-2"><GateChecks release={r} />{r.reason && <p className="ink-2">Reason: {r.reason}</p>}</div></td>
                    </tr>
                  </Fragment>
                ))}</tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </>
  );
}

function GateSettings({ id, gate, evaluators, onSaved }: { id: string; gate: Detail["gate"]; evaluators: Evaluator[]; onSaved: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState({ primary: gate.primary_evaluator_id ?? "", min: gate.min_score?.toString() ?? "", drop: gate.max_drop.toString() });
  const save = async () => {
    try { await api(`/api/prompts/id/${id}/gate`, { method: "PATCH", json: { primary_evaluator_id: form.primary || null, min_score: form.min === "" ? null : Number(form.min), max_drop: Number(form.drop) } }); toast("Gate saved."); onSaved(); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Could not save.", "bad"); }
  };
  return (
    <form className="section" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <div className="section__head"><h2>Release gate</h2><span className="label">What a version must pass before it can go live.</span></div>
      <div className="form-grid items-end">
        <label className="field"><span className="label">Primary evaluator</span><select className="select" value={form.primary} onChange={(e) => setForm({ ...form, primary: e.target.value })}>
          <option value="">None (no checks)</option>{evaluators.map((e) => (<option key={e.id} value={e.id}>{e.name}</option>))}</select></label>
        <label className="field"><span className="label">Minimum score (0 to 1)</span><input className="input" type="number" step="0.01" min={0} max={1} value={form.min} onChange={(e) => setForm({ ...form, min: e.target.value })} /></label>
        <label className="field"><span className="label">Allowed drop from the live version</span><input className="input" type="number" step="0.01" min={0} max={1} value={form.drop} onChange={(e) => setForm({ ...form, drop: e.target.value })} /></label>
        <div className="actions"><button className="btn">Save gate</button></div>
      </div>
    </form>
  );
}

function PromptsPage() {
  const { get, set } = useQueryState();
  const { user } = useMe();
  const { project } = useProject();
  const toast = useToast();
  const [form, setForm] = useState({ name: "", text: "" });
  const list = useApi<{ prompts: PromptRow[] }>(project ? `/api/prompts?project=${project.id}` : null);
  const id = get("prompt");
  const create = async () => {
    try { const r = await api<{ id: string }>("/api/prompts", { method: "POST", json: { project_id: project?.id, ...form } }); set({ prompt: r.id }); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Could not create the prompt.", "bad"); }
  };
  return (
    <Page title="Prompts" lede="Prompt text kept out of application code, with numbered versions and a gate before a version goes live.">
      <div className="split" data-selected={!!id}>
        <section className="split__list" aria-label="Prompts">
          <div className="split__bar"><h2>Prompts</h2><span className="label measured">{list.data ? `${list.data.prompts.length} in project` : ""}</span></div>
          {list.isLoading && <Loading />}
          {list.error && <div className="p-4"><ErrorState error={list.error} retry={() => list.mutate()} /></div>}
          {list.data?.prompts.length === 0 && <Empty title="No prompts in this project" />}
          {!!list.data?.prompts.length && (
            <table className="picklist">
              <thead><tr><th>Prompt</th><th className="num">Live</th><th className="num">Latest</th></tr></thead>
              <tbody>{list.data.prompts.map((p) => (
                <tr key={p.id} data-selected={p.id === id}>
                  <td><button className="pick pick--mono" aria-current={p.id === id ? "true" : undefined} onClick={() => set({ prompt: p.id })}>{p.name}</button></td>
                  <td className="num">{p.production_version == null ? "none" : `v${p.production_version}`}</td>
                  <td className="num">{p.latest_version == null ? "" : `v${p.latest_version}`}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </section>
        <section className="split__pane" aria-label={id ? "Prompt versions" : "New prompt"}>
          {!id && (
            <div className="split__body">
              {user?.role !== "viewer" ? (
                <form className="section" onSubmit={(e) => { e.preventDefault(); void create(); }}>
                  <div className="section__head"><h2>New prompt</h2><span className="label">Pick a prompt on the left to read its versions.</span></div>
                  <div className="form-grid">
                    <label className="field"><span className="label">Name (lower case, as applications will ask for it)</span><input className="input measured" required pattern="[a-z0-9][a-z0-9._-]*" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
                    <label className="field span-2"><span className="label">First version</span><textarea className="textarea measured" rows={3} required value={form.text} onChange={(e) => setForm({ ...form, text: e.target.value })} /></label>
                    <div className="actions"><button className="btn btn--primary">Create</button></div>
                  </div>
                </form>
              ) : <Empty title="No prompt open">Choose a prompt on the left to read its versions.</Empty>}
            </div>
          )}
          {id && <PromptView key={id} id={id} onClose={() => set({ prompt: null })} />}
        </section>
      </div>
    </Page>
  );
}

export default function Prompts() {
  return (<Suspense fallback={<Loading />}><PromptsPage /></Suspense>);
}
