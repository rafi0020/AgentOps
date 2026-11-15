"use client";

import { Suspense, useState } from "react";
import { useProject } from "@/components/Project";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { day } from "@/lib/format";
import { useQueryState } from "@/lib/url";

interface Dataset { id: string; name: string; description: string; items: number; created_at: string }
interface Item { id: string; input: unknown; expected: unknown; metadata: Record<string, unknown>; source_trace_id: string | null }
const show = (v: unknown) => (v == null ? "" : typeof v === "string" ? v : JSON.stringify(v));

function DatasetView({ id, name: listed, onClose }: { id: string; name?: string; onClose: () => void }) {
  const { user } = useMe();
  const toast = useToast();
  const { data, error, mutate } = useApi<{ dataset: { name: string }; total: number; items: Item[] }>(`/api/datasets/${id}/items?limit=200`);
  const [draft, setDraft] = useState({ input: "", expected: "" });
  const canEdit = user && user.role !== "viewer";
  const add = async () => {
    try { await api(`/api/datasets/${id}/items`, { method: "POST", json: [{ input: draft.input, expected: draft.expected || null }] }); setDraft({ input: "", expected: "" }); await mutate(); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Could not add the item.", "bad"); }
  };
  const upload = async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    try { const r = await api<{ added: number }>(`/api/datasets/${id}/items/upload`, { method: "POST", body: form }); toast(`${r.added} items added.`); await mutate(); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Upload failed.", "bad"); }
  };
  if (error) return <div className="split__body"><ErrorState error={error} retry={() => mutate()} /></div>;
  if (!data) return <div className="split__body"><Loading rows={6} /></div>;
  return (
    <>
      <div className="split__bar">
        <div className="min-w-0">
          <button className="btn btn--sm split__back" onClick={onClose}>All datasets</button>
          <h2>{data.dataset.name ?? listed}</h2>
        </div>
        <span className="label measured">{data.total} items</span>
      </div>
      <div className="split__body">
        {canEdit && (
          <form className="toolbar toolbar--wide" onSubmit={(e) => { e.preventDefault(); if (draft.input.trim()) void add(); }} aria-label="Add an item">
            <label className="field"><span className="label">Input</span><input className="input measured" value={draft.input} onChange={(e) => setDraft({ ...draft, input: e.target.value })} /></label>
            <label className="field"><span className="label">Expected (optional)</span><input className="input measured" value={draft.expected} onChange={(e) => setDraft({ ...draft, expected: e.target.value })} /></label>
            <button className="btn btn--primary" disabled={!draft.input.trim()}>Add item</button>
            <label className="btn">Upload a file<input type="file" accept=".csv,.json" className="visually-hidden" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} /></label>
          </form>
        )}
        {canEdit && <p className="legend-line">Upload a CSV with an input column (and optionally expected), or a JSON list.</p>}
        <div className="table-wrap">
          {data.items.length === 0 ? <Empty title="This dataset is empty">Add items above, upload a file, or create items from traces on the Traces screen.</Empty> : (
            <table className="table">
              <thead><tr><th>Input</th><th>Expected</th><th>Labels</th><th>From trace</th></tr></thead>
              <tbody>{data.items.map((i) => (
                <tr key={i.id}>
                  <td className="ink-2 measured">{show(i.input)}</td>
                  <td className="ink-2 measured">{show(i.expected)}</td>
                  <td><div className="chips">{Object.entries(i.metadata ?? {}).map(([k, v]) => (<span key={k} className="chip">{k}: {show(v)}</span>))}</div></td>
                  <td className="muted measured">{i.source_trace_id?.slice(0, 8) ?? ""}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </div>
      </div>
    </>
  );
}

function DatasetsPage() {
  const { get, set } = useQueryState();
  const { user } = useMe();
  const { project } = useProject();
  const toast = useToast();
  const [name, setName] = useState("");
  const list = useApi<{ datasets: Dataset[] }>(project ? `/api/datasets?project=${project.id}` : null);
  const id = get("dataset");
  const opened = list.data?.datasets.find((d) => d.id === id);
  const create = async () => {
    try { const r = await api<{ id: string }>("/api/datasets", { method: "POST", json: { project_id: project?.id, name } }); setName(""); set({ dataset: r.id }); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Could not create the dataset.", "bad"); }
  };
  return (
    <Page title="Datasets" lede="Test cases for experiments: inputs, and what a good answer contains.">
      <div className="split" data-selected={!!id}>
        <section className="split__list" aria-label="Datasets">
          <div className="split__bar"><h2>Datasets</h2><span className="label measured">{list.data ? `${list.data.datasets.length} in project` : ""}</span></div>
          {user?.role !== "viewer" && (
            <form className="list-form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) void create(); }}>
              <input className="input wide" placeholder="New dataset name" value={name} onChange={(e) => setName(e.target.value)} aria-label="New dataset name" />
              <button className="btn btn--primary" disabled={!name.trim()}>Create</button>
            </form>
          )}
          {list.isLoading && <Loading />}
          {list.error && <div className="p-4"><ErrorState error={list.error} retry={() => list.mutate()} /></div>}
          {list.data?.datasets.length === 0 && <Empty title="No datasets in this project" />}
          {!!list.data?.datasets.length && (
            <table className="picklist">
              <thead><tr><th>Dataset</th><th className="num">Items</th></tr></thead>
              <tbody>{list.data.datasets.map((d) => (
                <tr key={d.id} data-selected={d.id === id}>
                  <td>
                    <button className="pick" aria-current={d.id === id ? "true" : undefined} onClick={() => set({ dataset: d.id })}>{d.name}</button>
                    <div className="label">created {day(d.created_at)}</div>
                  </td>
                  <td className="num">{d.items}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </section>
        <section className="split__pane" aria-label="Items of the open dataset">
          {!id && <div className="split__none"><strong>No dataset open</strong>Choose a dataset to read and add its items.</div>}
          {id && <DatasetView key={id} id={id} name={opened?.name} onClose={() => set({ dataset: null })} />}
        </section>
      </div>
    </Page>
  );
}

export default function Datasets() {
  return (<Suspense fallback={<Loading />}><DatasetsPage /></Suspense>);
}
