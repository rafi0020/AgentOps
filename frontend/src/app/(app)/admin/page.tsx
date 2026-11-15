"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type FormEvent } from "react";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { when } from "@/lib/format";
import type { Role } from "@/lib/types";

interface Member { id: string; email: string; name: string; role: Role; is_active: boolean; created_at: string }
interface LLM { provider: string; model: string; base_url: string; fallback_model: string; price_input_per_mtok: number; price_output_per_mtok: number; api_key_set: boolean; source: string }
interface AuditEntry { id: number; at: string; user: string | null; action: string; target: string | null; detail: Record<string, unknown> | null; ip: string | null }
const TABS = [["projects", "Projects and keys"], ["users", "People"], ["model", "Model"], ["prices", "Prices"], ["ingest", "Failed batches"], ["audit", "Audit log"]] as const;
const message = (e: unknown) => (e instanceof ApiError ? e.message : "That did not work.");

function Users() {
  const { user: me } = useMe();
  const toast = useToast();
  const { data, error, isLoading, mutate } = useApi<{ users: Member[] }>("/api/users");
  const [form, setForm] = useState({ email: "", name: "", role: "engineer" as Role, password: "" });
  const [problem, setProblem] = useState("");
  const patch = async (id: string, json: Partial<Member>, done: string) => {
    try { await api(`/api/users/${id}`, { method: "PATCH", json }); toast(done); } catch (e) { toast(message(e), "bad"); }
    await mutate();
  };
  const add = async (event: FormEvent) => {
    event.preventDefault();
    setProblem("");
    try {
      await api("/api/users", { method: "POST", json: form });
      toast(`${form.email} can now sign in.`);
      setForm({ email: "", name: "", role: "engineer", password: "" });
      await mutate();
    } catch (e) { setProblem(message(e)); }
  };
  return (
    <>
      <div className="table-wrap">
        {isLoading && <Loading />}
        {error && <div className="p-4"><ErrorState error={error} retry={() => mutate()} /></div>}
        {data && (
          <table className="table">
            <thead><tr><th>Person</th><th>Role</th><th>Status</th><th>Added</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
            <tbody>
              {data.users.map((u) => (
                <tr key={u.id}>
                  <td>{u.name}<div className="label measured">{u.email}</div></td>
                  <td>
                    <select className="select" aria-label={`Role of ${u.email}`} value={u.role} onChange={(e) => patch(u.id, { role: e.target.value as Role }, `${u.email} is now ${e.target.value}.`)}>
                      <option value="viewer">viewer</option><option value="engineer">engineer</option><option value="admin">admin</option>
                    </select>
                  </td>
                  <td><StatusChip status={u.is_active ? "active" : "rejected"} label={u.is_active ? "active" : "deactivated"} /></td>
                  <td className="muted">{when(u.created_at)}</td>
                  <td className="text-right">
                    {u.id !== me?.id && <button className={`btn btn--sm ${u.is_active ? "btn--danger" : ""}`} onClick={() => patch(u.id, { is_active: !u.is_active }, u.is_active ? `${u.email} was signed out and deactivated.` : `${u.email} can sign in again.`)}>{u.is_active ? "Deactivate" : "Reactivate"}</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <form className="section" onSubmit={add}>
        <div className="section__head"><h2>Add a person</h2></div>
        <div className="form-grid">
          <label className="field"><span className="label">Name</span><input className="input" required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label className="field"><span className="label">E-mail</span><input className="input" type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></label>
          <label className="field"><span className="label">Role</span>
            <select className="select" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
              <option value="viewer">viewer: reads</option><option value="engineer">engineer: runs experiments and investigations</option><option value="admin">admin: approves and manages</option>
            </select>
          </label>
          <label className="field"><span className="label">Initial password</span><input className="input" type="password" required autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /><span className="hint">At least 10 characters, with a letter and a number.</span></label>
          {problem && <p className="hint hint--bad span-2" role="alert">{problem}</p>}
          <div className="span-2 actions"><button className="btn btn--primary">Add person</button></div>
        </div>
      </form>
    </>
  );
}

function Model() {
  const toast = useToast();
  const { data, error, isLoading, mutate } = useApi<LLM>("/api/settings/llm");
  const [form, setForm] = useState<LLM | null>(null);
  const [key, setKey] = useState("");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (data) setForm(data); }, [data]);
  if (isLoading || !form) return error ? <ErrorState error={error} retry={() => mutate()} /> : <div className="section"><Loading /></div>;
  const set = <K extends keyof LLM>(k: K, v: LLM[K]) => setForm({ ...form, [k]: v });
  const save = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const body = { ...form, api_key: key ? key : undefined };
      await mutate(await api<LLM>("/api/settings/llm", { method: "PUT", json: body }), false);
      setKey("");
      toast("Model settings saved.");
    } catch (e) { toast(message(e), "bad"); }
  };
  const test = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api<{ ok: boolean; error?: string; model?: string; latency_ms?: number }>("/api/settings/llm/test", { method: "POST" });
      setResult(r.ok ? { ok: true, text: `Connected to ${r.model} in ${r.latency_ms} ms.` } : { ok: false, text: r.error ?? "The test failed." });
    } catch (e) { setResult({ ok: false, text: message(e) }); }
    setBusy(false);
  };
  return (
    <form className="section" onSubmit={save}>
      <div className="section__head"><h2>Model provider</h2><span className="chip">{form.source === "organisation" ? "set for this organisation" : "using the server default"}</span></div>
      <div className="form-grid">
        <label className="field"><span className="label">Provider</span>
          <select className="select" value={form.provider} onChange={(e) => set("provider", e.target.value)}>
            <option value="openai_compat">OpenAI-compatible (OpenAI, Gemini, Groq, 9Router, any gateway)</option>
            <option value="anthropic">Anthropic</option><option value="ollama">Ollama (local)</option><option value="stub">Built-in demonstration script (no model)</option>
          </select>
        </label>
        <label className="field"><span className="label">Model</span><input className="input measured" required value={form.model} onChange={(e) => set("model", e.target.value)} /></label>
        <label className="field"><span className="label">Base URL</span><input className="input measured" value={form.base_url} onChange={(e) => set("base_url", e.target.value)} placeholder={form.provider === "ollama" ? "http://host.docker.internal:11434/v1" : "https://api.example.com/v1"} /></label>
        <label className="field"><span className="label">Fallback model (optional)</span><input className="input measured" value={form.fallback_model} onChange={(e) => set("fallback_model", e.target.value)} /></label>
        <label className="field span-2"><span className="label">API key</span>
          <input className="input measured" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder={form.api_key_set ? "A key is stored. Type a new one to replace it." : "No key stored"} />
          <span className="hint">Stored encrypted. It is never shown again and never sent to the browser.</span>
        </label>
        <label className="field"><span className="label">Price per million input tokens, USD</span><input className="input measured" type="number" min={0} step="0.01" value={form.price_input_per_mtok} onChange={(e) => set("price_input_per_mtok", Number(e.target.value))} /></label>
        <label className="field"><span className="label">Price per million output tokens, USD</span><input className="input measured" type="number" min={0} step="0.01" value={form.price_output_per_mtok} onChange={(e) => set("price_output_per_mtok", Number(e.target.value))} /></label>
        <p className="hint span-2">Prices are used to estimate cost and enforce budgets. Set them to what your provider charges.</p>
        <div className="span-2 actions">
          <button className="btn btn--primary">Save</button>
          <button type="button" className="btn" onClick={test} disabled={busy} aria-busy={busy}>Test the saved settings</button>
          {result && <span className={result.ok ? "ok" : "bad"} role="status">{result.text}</span>}
        </div>
      </div>
    </form>
  );
}

function Audit() {
  const [action, setAction] = useState("");
  const [before, setBefore] = useState<number | null>(null);
  const { data, error, isLoading, mutate } = useApi<{ entries: AuditEntry[]; next_before: number | null }>(`/api/audit?limit=50&action=${encodeURIComponent(action)}${before ? `&before=${before}` : ""}`, { keepPreviousData: true });
  return (
    <>
      <div className="section__form section__form--flush">
        <label className="field"><span className="label">Kind of entry</span>
          <select className="select" value={action} onChange={(e) => { setAction(e.target.value); setBefore(null); }}>
            <option value="">Everything</option><option value="auth.">Sign-ins</option><option value="user.">People</option><option value="key.">Keys</option><option value="project.">Projects</option>
            <option value="release.">Releases</option><option value="prompt.">Prompts</option><option value="experiment.">Experiments</option><option value="run.">Investigations</option>
            <option value="approval.">Approvals</option><option value="monitor.">Monitors</option><option value="settings.">Settings</option>
          </select>
        </label>
        {before && <button className="btn" onClick={() => setBefore(null)}>Back to newest</button>}
      </div>
      <div className="table-wrap">
        {isLoading && !data && <Loading />}
        {error && <div className="p-4"><ErrorState error={error} retry={() => mutate()} /></div>}
        {data?.entries.length === 0 && <Empty title="No entries">Nothing of this kind has been recorded.</Empty>}
        {!!data?.entries.length && (
          <table className="table">
            <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th><th>Address</th></tr></thead>
            <tbody>
              {data.entries.map((e) => (
                <tr key={e.id}>
                  <td className="muted measured">{when(e.at)}</td><td>{e.user ?? "deleted user"}</td><td className="measured">{e.action}</td>
                  <td className="ink-2 measured">{e.detail ? JSON.stringify(e.detail).slice(0, 160) : ""}</td><td className="muted measured">{e.ip}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {data?.next_before && <div><button className="btn" onClick={() => setBefore(data.next_before)}>Older entries</button></div>}
    </>
  );
}

interface ProjectRow { id: string; name: string; retention_days: number }
interface KeyRow { id: string; name: string; prefix: string; scopes: string[]; revoked: boolean; last_used_at: string | null; created_at: string }

function Keys({ project }: { project: ProjectRow }) {
  const toast = useToast();
  const { data, mutate } = useApi<{ keys: KeyRow[] }>(`/api/projects/${project.id}/keys`);
  const [created, setCreated] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", prompts: false });
  const create = async () => {
    try {
      const r = await api<{ key: string }>(`/api/projects/${project.id}/keys`, { method: "POST", json: { name: form.name, scopes: form.prompts ? ["ingest", "prompts:read"] : ["ingest"] } });
      setCreated(r.key); setForm({ name: "", prompts: false }); await mutate();
    } catch (e) { toast(message(e), "bad"); }
  };
  return (
    <div className="grid gap-3">
      {created && (
        <div className="banner banner--instrument" role="status">
          <div className="grid gap-1 w-full"><strong>Copy this key now. It will not be shown again.</strong><div className="well code">{created}</div>
            <span className="hint">Applications send it as: Authorization: Bearer {"<key>"}, to /v1/traces.</span>
            <div><button className="btn btn--sm" onClick={() => { void navigator.clipboard?.writeText(created); toast("Copied."); }}>Copy</button> <button className="btn btn--sm" onClick={() => setCreated(null)}>Done</button></div></div>
        </div>
      )}
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Key</th><th>Scopes</th><th>Last used</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
          <tbody>{data?.keys.map((k) => (
            <tr key={k.id}><td>{k.name}<div className="label measured">{k.prefix}…</div></td><td className="muted">{k.scopes.join(", ")}</td>
              <td className="muted">{k.last_used_at ? when(k.last_used_at) : "never"}</td>
              <td className="text-right">{k.revoked ? <span className="chip">revoked</span> : <button className="btn btn--sm btn--danger" onClick={async () => { if (confirm(`Revoke ${k.name}? Applications using it stop being able to send at once.`)) { try { await api(`/api/projects/${project.id}/keys/${k.id}`, { method: "DELETE" }); toast("Revoked."); await mutate(); } catch (e) { toast(message(e), "bad"); } } }}>Revoke</button>}</td></tr>
          ))}</tbody>
        </table>
      </div>
      <form className="section__form section__form--flush" onSubmit={(e) => { e.preventDefault(); void create(); }}>
        <label className="field"><span className="label">New key name</span><input className="input" required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.prompts} onChange={(e) => setForm({ ...form, prompts: e.target.checked })} />Can also read live prompts</label>
        <button className="btn btn--primary">Create key</button>
      </form>
    </div>
  );
}

function Projects() {
  const toast = useToast();
  const { data, mutate } = useApi<{ projects: ProjectRow[] }>("/api/projects");
  const [name, setName] = useState("");
  return (
    <>
      {data?.projects.map((p) => (
        <section key={p.id} className="section" aria-label={p.name}>
          <div className="section__head"><h2>{p.name}</h2>
            <label className="flex items-center gap-2 label">Keep data for
              <select className="select" value={p.retention_days} onChange={async (e) => { try { await api(`/api/projects/${p.id}`, { method: "PATCH", json: { retention_days: Number(e.target.value) } }); await mutate(); toast("Retention saved."); } catch (x) { toast(message(x), "bad"); } }}>
                {[7, 14, 30, 60, 90, 180, 365].map((d) => (<option key={d} value={d}>{d} days</option>))}{![7, 14, 30, 60, 90, 180, 365].includes(p.retention_days) && <option value={p.retention_days}>{p.retention_days} days</option>}
              </select></label></div>
          <Keys project={p} />
        </section>
      ))}
      <form className="section__form" onSubmit={async (e) => { e.preventDefault(); try { await api("/api/projects", { method: "POST", json: { name } }); setName(""); await mutate(); } catch (x) { toast(message(x), "bad"); } }}>
        <input className="input wide" placeholder="New project name" aria-label="New project name" value={name} onChange={(e) => setName(e.target.value)} />
        <button className="btn btn--primary" disabled={!name.trim()}>Create project</button>
      </form>
    </>
  );
}

function Prices() {
  const toast = useToast();
  const { data, mutate } = useApi<{ prices: { model: string; input_per_mtok: number; output_per_mtok: number }[] }>("/api/prices");
  const [form, setForm] = useState({ model: "", input: "", output: "" });
  return (
    <>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Model</th><th className="num">Input, USD per million tokens</th><th className="num">Output, USD per million tokens</th></tr></thead>
          <tbody>{data?.prices.map((p) => (<tr key={p.model}><td className="measured">{p.model}</td><td className="num">{p.input_per_mtok}</td><td className="num">{p.output_per_mtok}</td></tr>))}</tbody>
        </table>
      </div>
      <form className="section__form" onSubmit={async (e) => { e.preventDefault(); try { await api("/api/prices", { method: "PUT", json: { model: form.model, input_per_mtok: Number(form.input), output_per_mtok: Number(form.output) } }); setForm({ model: "", input: "", output: "" }); await mutate(); toast("Price saved."); } catch (x) { toast(message(x), "bad"); } }}>
        <label className="field wide"><span className="label">Model (as reported in spans)</span><input className="input measured" required value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} /></label>
        <label className="field"><span className="label">Input</span><input className="input" type="number" step="any" min={0} required value={form.input} onChange={(e) => setForm({ ...form, input: e.target.value })} /></label>
        <label className="field"><span className="label">Output</span><input className="input" type="number" step="any" min={0} required value={form.output} onChange={(e) => setForm({ ...form, output: e.target.value })} /></label>
        <button className="btn btn--primary">Save</button>
      </form>
    </>
  );
}

function DeadBatches() {
  const toast = useToast();
  const { data, mutate } = useApi<{ pending: number; dead: { id: string; span_count: number; error: string; received_at: string }[] }>("/api/ingest/dead");
  const act = async (path: string, done: string) => { try { await api(path, { method: "POST" }); toast(done); await mutate(); } catch (e) { toast(message(e), "bad"); } };
  return (
    <>
      <div className="section__head"><span className="label">{data ? `${data.pending} waiting to be processed` : ""}</span></div>
      {data?.dead.length === 0 && <Empty title="None">Every received batch has been stored.</Empty>}
      {!!data?.dead.length && (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Received</th><th className="num">Spans</th><th>Error</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
            <tbody>{data.dead.map((d) => (
              <tr key={d.id}><td className="muted">{when(d.received_at)}</td><td className="num">{d.span_count}</td><td className="ink-2 measured">{d.error}</td>
                <td className="text-right"><div className="flex gap-1 justify-end"><button className="btn btn--sm" onClick={() => act(`/api/ingest/dead/${d.id}/replay`, "Queued again.")}>Retry</button>
                  <button className="btn btn--sm btn--danger" onClick={() => { if (confirm("Discard this batch? Its spans will not be stored.")) void act(`/api/ingest/dead/${d.id}/discard`, "Discarded."); }}>Discard</button></div></td></tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </>
  );
}

function AdminPage() {
  const router = useRouter();
  const tab = useSearchParams().get("tab") ?? "projects";
  const { user } = useMe();
  if (user && user.role !== "admin") return <Page title="Administration"><div className="panel"><Empty title="Administrators only">Ask an administrator of your organisation for access.</Empty></div></Page>;
  const current = TABS.find(([value]) => value === tab) ? tab : "projects";
  return (
    <Page title="Administration" lede="Projects and ingestion keys, people, the model the Investigator and judges use, prices, failed batches, and the record of who did what.">
      <div className="admin">
        <nav aria-label="Administration sections">
          <div className="admin__nav" role="tablist" aria-orientation="vertical">
            {TABS.map(([value, label]) => (<button key={value} role="tab" aria-selected={current === value} onClick={() => router.replace(`/admin/?tab=${value}`)}>{label}</button>))}
          </div>
        </nav>
        <div className="admin__body">
          {current === "projects" && <Projects />}
          {current === "prices" && <Prices />}
          {current === "ingest" && <DeadBatches />}
          {current === "users" && <Users />}
          {current === "model" && <Model />}
          {current === "audit" && <Audit />}
        </div>
      </div>
    </Page>
  );
}

export default function AdminRoute() {
  return (<Suspense fallback={<Loading />}><AdminPage /></Suspense>);
}
