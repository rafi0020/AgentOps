"use client";

import { Suspense } from "react";
import { useProject, withProject } from "@/components/Project";
import { Page } from "@/components/Shell";
import { rangeOf } from "@/components/TimeRange";
import { Empty, ErrorState, Loading } from "@/components/ui";
import { useApi } from "@/lib/api";
import { ms, time, tokens, usd } from "@/lib/format";
import type { TraceRow } from "@/lib/types";
import { useQueryState } from "@/lib/url";

interface Session { session_id: string; traces: number; first: string; last: string; cost_usd: number; errors: number; tokens: number }

const clock = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
/** Time from the first trace to the last, as the session itself spans it. */
const length = (s: Session) => ms(new Date(s.last).getTime() - new Date(s.first).getTime());

function SessionsPage() {
  const { get, set } = useQueryState();
  const { project } = useProject();
  const range = rangeOf(get("range") ?? "7d");
  const session = get("session");
  const list = useApi<{ sessions: Session[] }>(project ? `/api/sessions?project=${project.id}&from=${encodeURIComponent(range.from)}&to=${encodeURIComponent(range.to)}` : null);
  const one = useApi<{ traces: TraceRow[] }>(project && session ? `/api/sessions/${project.id}/${encodeURIComponent(session)}` : null);
  const open = list.data?.sessions.find((s) => s.session_id === session);

  return (
    <Page title="Sessions" lede="Traces grouped by the session.id attribute an application sends. Pick one to read its turns in order.">
      <div className="split" data-selected={!!session}>
        <section className="split__list" aria-label="Sessions in this range">
          <div className="split__bar">
            <h2>Sessions</h2>
            <span className="label measured">{list.data ? `${list.data.sessions.length} in range` : ""}</span>
          </div>
          {list.isLoading && <Loading />}
          {list.error && <div className="p-4"><ErrorState error={list.error} retry={() => list.mutate()} /></div>}
          {list.data?.sessions.length === 0 && <Empty title="No sessions in this range">Applications group traces into sessions by sending a session.id attribute.</Empty>}
          {!!list.data?.sessions.length && (
            <table className="picklist">
              <thead><tr><th>Session</th><th className="num">Turns</th><th className="num">Length</th><th>Status</th></tr></thead>
              <tbody>
                {list.data.sessions.map((s) => (
                  <tr key={s.session_id} data-selected={s.session_id === session}>
                    <td>
                      <button className="pick pick--mono" aria-current={s.session_id === session ? "true" : undefined} onClick={() => set({ session: s.session_id })}>{s.session_id}</button>
                      <div className="label measured">{time(s.last)}</div>
                    </td>
                    <td className="num">{s.traces}</td>
                    <td className="num">{length(s)}</td>
                    <td className={s.errors ? "bad" : "ok"}>{s.errors ? `failed ${s.errors}` : "ok"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="split__pane" aria-label="Turns of the open session">
          {!session && (
            <div className="split__none"><strong>No session open</strong>Choose a session to read its turns, oldest first. Each turn links to its trace.</div>
          )}
          {session && (
            <>
              <div className="split__bar">
                <div className="min-w-0">
                  <button className="btn btn--sm split__back" onClick={() => set({ session: null })}>All sessions</button>
                  <h2 className="measured">{session}</h2>
                </div>
                {open && (
                  <span className="label measured">{open.traces} {open.traces === 1 ? "turn" : "turns"}, {length(open)}, {tokens(open.tokens)} tokens, {usd(open.cost_usd)}</span>
                )}
              </div>
              <div className="split__body">
                {one.isLoading && <Loading rows={6} />}
                {one.error && <ErrorState error={one.error} retry={() => one.mutate()} />}
                {one.data && one.data.traces.length === 0 && <Empty title="No turns in this session" />}
                {!!one.data?.traces.length && (
                  <ol className="stream" aria-label="Turns, oldest first">
                    {one.data.traces.map((t, i) => (
                      <li key={t.trace_id} className="stream__row" data-status={t.status}>
                        <span className="stream__seq">T{i + 1}</span>
                        <span className="stream__time">{clock(t.start_time)}</span>
                        <span className="stream__what">
                          <a href={withProject(`/traces/?trace=${t.trace_id}`, project?.id)}>{t.root_name}</a>
                          {t.status === "error" && <span className="stream__state"> failed ({t.error_count})</span>}
                        </span>
                        <span className="stream__meta">{ms(t.duration_ms)} · {tokens(t.tokens)} tok · {usd(t.cost_usd)}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            </>
          )}
        </section>
      </div>
    </Page>
  );
}

export default function Sessions() {
  return (<Suspense fallback={<Loading />}><SessionsPage /></Suspense>);
}
