"use client";

import { Fragment, useState } from "react";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { when } from "@/lib/format";
import type { Approval, Release } from "@/lib/types";
import { GateChecks } from "@/components/GateChecks";

/** A chevron that turns to show whether a row's details are open. */
function Chevron({ open }: { open: boolean }) {
  return (
    <svg className="chev" data-open={open} viewBox="0 0 16 16" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 3.5 10.5 8 6 12.5" />
    </svg>
  );
}

export default function ApprovalsPage() {
  const { user } = useMe();
  const toast = useToast();
  const [tab, setTab] = useState<"pending" | "all">("pending");
  const [reason, setReason] = useState<Record<string, string>>({});
  const [toggled, setToggled] = useState<Record<string, boolean>>({});
  const { data, error, isLoading, mutate } = useApi<{ approvals: Approval[] }>(`/api/approvals?status=${tab}`);
  const admin = user?.role === "admin";
  // Waiting items open by themselves, so what is being approved is in view; a row can be closed.
  const isOpen = (id: string, status: string) => toggled[id] ?? status === "pending";
  const toggle = (id: string, status: string) => setToggled({ ...toggled, [id]: !isOpen(id, status) });
  const decide = async (a: Approval, decision: "approved" | "rejected") => {
    try {
      await api(`/api/approvals/${a.id}/decide`, { method: "POST", json: { decision, reason: reason[a.id] || null } });
      toast(decision === "approved" ? "Approved. The agent will carry it out." : "Rejected. The agent has been told.");
      await mutate();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That did not work.", "bad");
      await mutate();
    }
  };
  return (
    <Page title="Approvals" lede={admin ? "Actions the agent has proposed. Nothing happens until you decide." : "Actions you asked the agent to take that are waiting for an administrator."}>
      <div className="tabs" role="tablist">
        <button className="tab" role="tab" aria-selected={tab === "pending"} onClick={() => setTab("pending")}>Waiting</button>
        <button className="tab" role="tab" aria-selected={tab === "all"} onClick={() => setTab("all")}>All decisions</button>
      </div>
      {error && <ErrorState error={error} retry={() => mutate()} />}
      {isLoading && <div className="panel"><Loading /></div>}
      {data?.approvals.length === 0 && <div className="panel"><Empty title={tab === "pending" ? "Nothing is waiting" : "No decisions have been made yet"}>Requests appear here when the agent proposes an action such as creating an alert rule.</Empty></div>}
      {!!data?.approvals.length && (
        <div className="table-wrap">
          <table className="table queue">
            <thead><tr><th>Proposed action</th><th>Asked by</th><th>When</th><th>Status</th><th><span className="visually-hidden">Decide</span></th></tr></thead>
            <tbody>
              {data.approvals.map((a) => {
                const open = isOpen(a.id, a.status);
                return (
                  <Fragment key={a.id}>
                    <tr>
                      <td>
                        <button className="detail-toggle" aria-expanded={open} aria-controls={`ap-${a.id}`} onClick={() => toggle(a.id, a.status)}>
                          <Chevron open={open} />{a.tool.replace(/_/g, " ")}
                        </button>
                        <div className="label"><a href={`/investigations/?run=${a.run_id}`}>{a.question}</a></div>
                      </td>
                      <td><span className="person">{a.requested_by ?? "Someone"}</span></td>
                      <td className="muted measured">{when(a.created_at)}</td>
                      <td><StatusChip status={a.status} /></td>
                      <td>
                        {a.status === "pending" && admin && (
                          <div className="actions">
                            <button className="btn btn--sm btn--primary" onClick={() => decide(a, "approved")}>Approve</button>
                            <button className="btn btn--sm btn--danger" onClick={() => decide(a, "rejected")}>Reject</button>
                          </div>
                        )}
                      </td>
                    </tr>
                    {open && (
                      <tr className="detail" id={`ap-${a.id}`}>
                        <td colSpan={5}>
                          <div className="grid gap-3">
                            <div>
                              <div className="label mb-1">What the agent wants to do</div>
                              <dl className="kv">
                                {Object.entries(a.arguments).map(([k, v]) => (<Fragment key={k}><dt>{k.replace(/_/g, " ")}</dt><dd>{v == null ? "whole company" : String(v)}</dd></Fragment>))}
                              </dl>
                            </div>
                            {a.status !== "pending" && a.reason && <p className="ink-2">Reason given: {a.reason}</p>}
                            {a.status === "pending" && admin && (
                              <label className="field max-w-md"><span className="label">Reason (optional, shown to the agent if rejected)</span>
                                <input className="input" maxLength={500} value={reason[a.id] ?? ""} onChange={(e) => setReason({ ...reason, [a.id]: e.target.value })} /></label>
                            )}
                            {a.status === "pending" && !admin && <p className="muted">An administrator decides this request.</p>}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <ReleaseQueue />
    </Page>
  );
}

function ReleaseQueue() {
  const { user } = useMe();
  const toast = useToast();
  const [reason, setReason] = useState<Record<string, string>>({});
  const [toggled, setToggled] = useState<Record<string, boolean>>({});
  const { data, mutate } = useApi<{ releases: Release[] }>("/api/releases?status=pending");
  const decide = async (r: Release, decision: "approved" | "rejected") => {
    try { await api(`/api/releases/${r.id}/decide`, { method: "POST", json: { decision, reason: reason[r.id] || null } }); toast(decision === "approved" ? `Version ${r.to_version} is now live.` : "Rejected."); }
    catch (e) { toast(e instanceof ApiError ? e.message : "That did not work.", "bad"); }
    await mutate();
  };
  if (!data?.releases.length) return null;
  return (
    <section className="section" aria-labelledby="releases-h">
      <div className="section__head"><h2 id="releases-h">Prompt releases waiting</h2><span className="label">A version goes live only when an administrator other than its requester approves it.</span></div>
      <div className="table-wrap">
        <table className="table queue">
          <thead><tr><th>Prompt and version</th><th>Gate</th><th>Requested by</th><th><span className="visually-hidden">Decide</span></th></tr></thead>
          <tbody>
            {data.releases.map((r) => {
              const open = toggled[r.id] ?? true;
              const passed = r.gate.passed;
              return (
                <Fragment key={r.id}>
                  <tr>
                    <td>
                      <button className="detail-toggle" aria-expanded={open} aria-controls={`rel-${r.id}`} onClick={() => setToggled({ ...toggled, [r.id]: !open })}>
                        <Chevron open={open} />{r.prompt}: {r.from_version == null ? `version ${r.to_version} (first release)` : `version ${r.from_version} to ${r.to_version}`}
                      </button>
                    </td>
                    <td><span className={passed ? "ok" : "bad"}>{passed ? "gate passed" : "gate failed"}</span></td>
                    <td className="muted">{r.requested_by ?? ""}</td>
                    <td>
                      {user?.role === "admin" ? (
                        <div className="actions">
                          <button className="btn btn--sm btn--primary" onClick={() => decide(r, "approved")}>Approve and release</button>
                          <button className="btn btn--sm btn--danger" onClick={() => decide(r, "rejected")}>Reject</button>
                        </div>
                      ) : <span className="muted">Waiting for an administrator</span>}
                    </td>
                  </tr>
                  {open && (
                    <tr className="detail" id={`rel-${r.id}`}>
                      <td colSpan={4}>
                        <div className="grid gap-3">
                          <GateChecks release={r} />
                          {user?.role === "admin" && (
                            <label className="field max-w-md"><span className="label">Reason (optional)</span>
                              <input className="input" value={reason[r.id] ?? ""} onChange={(e) => setReason({ ...reason, [r.id]: e.target.value })} /></label>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
