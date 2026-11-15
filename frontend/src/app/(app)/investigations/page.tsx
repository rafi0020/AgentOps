"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { EvidenceDrawer } from "@/components/Evidence";
import { ReportView } from "@/components/ReportView";
import { RunTrail } from "@/components/RunTrail";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { STATUS_LABEL, usd, when } from "@/lib/format";
import { followRun } from "@/lib/sse";
import type { Run } from "@/lib/types";

const EXAMPLES = [
  "Why did errors rise in checkout-assistant around 2026-10-14?",
  "Why did support-triage get slower in the last two weeks?",
  "Why did the cost per trace of doc-search jump?",
  "Was there a provider outage in the last 30 days, and which projects did it affect?",
];
const ACTIVE = ["queued", "running"];
const FINAL = ["completed", "failed", "cancelled", "budget_exceeded"];

/** The page frame: the console on the left, the history of runs on the right. */
function Frame({ title, lede, actions, side, children }: { title: string; lede?: string; actions?: ReactNode; side: ReactNode; children: ReactNode }) {
  return (
    <Page title={title} lede={lede} actions={actions}>
      <div className="split split--end">
        <div className="split__pane">{children}</div>
        {side}
      </div>
    </Page>
  );
}

function History({ runs, activeId }: { runs: { data?: { runs: Run[] }; isLoading: boolean; error?: Error | ApiError; mutate: () => void }; activeId?: string | null }) {
  return (
    <section className="split__list" aria-label="Recent investigations">
      <div className="split__bar"><h2>Recent investigations</h2><span className="label measured">{runs.data ? `${runs.data.runs.length} shown` : ""}</span></div>
      {runs.isLoading && <Loading rows={5} />}
      {runs.error && <div className="p-4"><ErrorState error={runs.error} retry={() => runs.mutate()} /></div>}
      {runs.data?.runs.length === 0 && <Empty title="No questions yet">Ask one; it will appear here with its result.</Empty>}
      {!!runs.data?.runs.length && (
        <table className="picklist">
          <thead><tr><th>Question</th><th>Status</th></tr></thead>
          <tbody>
            {runs.data.runs.map((r) => (
              <tr key={r.id} data-selected={r.id === activeId}>
                <td>
                  <a className="pick pick--clamp" href={`/investigations/?run=${r.id}`} aria-current={r.id === activeId ? "true" : undefined}>{r.question}</a>
                  <div className="label">{r.asked_by} · {when(r.created_at)} · {usd(r.cost_usd)}</div>
                </td>
                <td><StatusChip status={r.status} label={STATUS_LABEL[r.status]} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function AskForm({ side }: { side: ReactNode }) {
  const { user } = useMe();
  const router = useRouter();
  const toast = useToast();
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (question.trim().length < 3) return;
    setBusy(true);
    try {
      const { id } = await api<{ id: string }>("/api/investigations", { method: "POST", json: { question } });
      router.push(`/investigations/?run=${id}`);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Could not start the run.", "bad");
      setBusy(false);
    }
  }

  return (
    <Frame title="Investigations" lede="Ask why something changed. The Investigator reads metrics, traces, errors and releases, shows each step, and links every figure to the result it came from." side={side}>
      <div className="split__bar"><h2>Ask the agent</h2>{user?.role !== "viewer" && <span className="label">Ctrl + Enter to send</span>}</div>
      <div className="split__body">
        {user?.role === "viewer" && <Empty title="Read only">Viewers can read past investigations. Ask an engineer to run a new one.</Empty>}
        {user?.role !== "viewer" && (
          <>
            <form className="ask" onSubmit={submit}>
              <div className="prompt-line">
                <span className="prompt-line__sigil" aria-hidden>&gt;</span>
                <label className="field">
                  <span className="visually-hidden">Your question</span>
                  <textarea className="textarea measured" rows={3} maxLength={2000} value={question} onChange={(e) => setQuestion(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) e.currentTarget.form?.requestSubmit(); }}
                    placeholder="Why did errors rise in checkout-assistant yesterday?" />
                </label>
                <div className="prompt-line__foot">
                  <span className="hint">{question.length} of 2000</span>
                  <button className="btn btn--primary" disabled={busy || question.trim().length < 3} aria-busy={busy}>Ask the agent</button>
                </div>
              </div>
            </form>
            <section className="section" aria-labelledby="examples-h">
              <div className="section__head"><h2 id="examples-h">Try one of these</h2></div>
              <div className="chips">
                {EXAMPLES.map((q) => (<button key={q} className="btn btn--sm" onClick={() => setQuestion(q)}>{q}</button>))}
              </div>
            </section>
          </>
        )}
      </div>
    </Frame>
  );
}

function RunView({ id, side }: { id: string; side: ReactNode }) {
  const { user } = useMe();
  const toast = useToast();
  const { data: run, error, mutate } = useApi<Run>(`/api/investigations/${id}`);
  const [evidence, setEvidence] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const live = run ? ACTIVE.includes(run.status) : false;
  // A run waiting for an answer or an approval is not finished: keep listening, so the page moves on by
  // itself when someone else (an administrator approving, say) lets it continue.
  const unfinished = run ? !FINAL.includes(run.status) : false;

  // Until the run is finished, every event from the server refreshes it. Reconnection is automatic.
  useEffect(() => (unfinished ? followRun(id, () => void mutate()) : undefined), [id, unfinished, mutate]);

  if (error) return <Frame title="Run" side={side}><div className="split__body"><ErrorState error={error} retry={() => mutate()} /></div></Frame>;
  if (!run) return <Frame title="Run" side={side}><div className="split__body"><Loading rows={8} /></div></Frame>;
  const mine = user?.role === "admin" || user?.id === run.user_id;
  const act = async (path: string, json: unknown, done: string) => {
    try {
      await api(path, { method: "POST", json });
      toast(done);
      await mutate();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That did not work.", "bad");
    }
  };

  return (
    <Frame
      title={run.question}
      side={side}
      actions={
        <>
          <StatusChip status={run.status} label={STATUS_LABEL[run.status]} />
          {mine && (live || run.status.startsWith("waiting")) && <button className="btn btn--danger" onClick={() => act(`/api/investigations/${id}/cancel`, undefined, "Cancelling the run.")}>Cancel</button>}
          <a className="btn" href="/investigations/">New question</a>
        </>
      }
    >
      <div className="split__body">
        {run.status === "waiting_user" && (
          <form className="banner banner--person" onSubmit={(e) => { e.preventDefault(); void act(`/api/investigations/${id}/answer`, { text: answer }, "Answer sent; the agent is continuing."); setAnswer(""); }}>
            <div className="grid gap-2 w-full">
              <strong>The agent needs one thing from you: {run.pending?.question}</strong>
              {mine ? (
                <div className="flex gap-2">
                  <input className="input" value={answer} onChange={(e) => setAnswer(e.target.value)} maxLength={1000} aria-label="Your answer" autoFocus />
                  <button className="btn btn--primary" disabled={!answer.trim()}>Send answer</button>
                </div>
              ) : <span>Only the person who asked, or an administrator, can answer.</span>}
            </div>
          </form>
        )}
        {run.status === "waiting_approval" && (
          <div className="banner banner--person">
            <span>The agent proposed an action ({run.pending?.tool?.replace(/_/g, " ")}) that needs an administrator&apos;s approval. Nothing has been changed.</span>
            <a className="btn" href="/approvals/">Open approvals</a>
          </div>
        )}
        {run.error && <div className="banner banner--bad" role="alert">{run.error}</div>}

        <div className="run-grid">
          <div className="report-sheet">
            {run.report ? <ReportView report={run.report} onEvidence={setEvidence} /> : (
              <div className="panel">
                {live ? <Loading rows={5} label="The agent is working" /> : unfinished ? <Empty title="Paused">The report will appear here once the run continues.</Empty> : <Empty title="No report">{run.status === "cancelled" ? "This run was cancelled before it finished." : "The run ended without a report. The trail shows how far it got."}</Empty>}
              </div>
            )}
          </div>
          <section className="console-pane" aria-label="Run log">
            <div className="console-pane__bar"><h2>Run log</h2><span className="label measured">{run.steps_used} model steps, {run.tokens_used.toLocaleString()} tokens, est. {usd(run.cost_usd)}</span></div>
            <div className="console-pane__body"><RunTrail run={run} onEvidence={setEvidence} /></div>
          </section>
        </div>
        {evidence && <EvidenceDrawer runId={id} handle={evidence} onClose={() => setEvidence(null)} />}
      </div>
    </Frame>
  );
}

function AskPage() {
  const run = useSearchParams().get("run");
  const history = useApi<{ runs: Run[] }>("/api/investigations?limit=15");
  const side = <History runs={history} activeId={run} />;
  return run ? <RunView key={run} id={run} side={side} /> : <AskForm side={side} />;
}

export default function Ask() {
  return (
    <Suspense fallback={<Loading />}>
      <AskPage />
    </Suspense>
  );
}
