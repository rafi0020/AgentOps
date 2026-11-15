"use client";

import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, Spark } from "@/components/ui";
import { useApi } from "@/lib/api";
import { day, pct, usd } from "@/lib/format";

interface EvalRun {
  name: string; model: string; split: string; at: string; n: number;
  metrics: { answer_accuracy: number; root_cause_rate: number | null; behaviour_rate: number | null; unsupported_numbers_per_report: number; mean_steps: number; mean_cost_usd: number; p50_latency_s: number; p95_latency_s: number; completed_rate: number };
  by_kind: Record<string, { n: number; passed: number }>;
  note?: string; regraded_at?: string;
}
interface AttackRun { model: string; at: string; note?: string; cases: { id: string; name: string; passed: boolean; result?: "held" | "failed" | "inconclusive"; detail: string }[] }
interface Unavailable { name: string; model: string; at: string; n: number; note: string }

const setName = (r: EvalRun) => (r.name.startsWith("seen-") ? "held out, re-asked after a fix" : r.split === "test" ? "held out" : "development");
const RESULT_TONE = { held: "ok", failed: "bad", inconclusive: "" } as const;

/** The same measure for this model across its earlier runs: a trend, drawn only when there are two or more. */
function trendOf(results: EvalRun[], run: EvalRun, pick: (r: EvalRun) => number | null | undefined): (number | null)[] {
  return results.filter((r) => r.model === run.model).sort((x, y) => (x.at < y.at ? -1 : 1)).map((r) => pick(r) ?? null);
}

function Cell({ name, value, caption, trend, tone, label }: { name: string; value: string; caption?: string; trend?: (number | null)[]; tone?: "ok" | "bad"; label: string }) {
  const series = trend ?? [];
  return (
    <div role="listitem">
      <span className="board__name">{name}</span>
      <span className={`board__value ${tone ? `board__value--${tone}` : ""}`}>{value}</span>
      {series.filter((v) => v != null).length >= 2 && <Spark values={series} label={`${label}, trend across ${series.length} runs of this model`} />}
      {caption && <span className="board__caption">{caption}</span>}
    </div>
  );
}

export default function EvaluationPage() {
  const { data, error, isLoading, mutate } = useApi<{ results: EvalRun[]; attacks: AttackRun[]; unavailable?: Unavailable[] }>("/api/eval/results");
  const results = data?.results ?? [];
  return (
    <Page title="Evaluation" lede="How the Investigator scored on questions about incidents planted in the demo data, and whether it withstood a set of attacks. Results are shown as recorded, including failures.">
      {error && <ErrorState error={error} retry={() => mutate()} />}
      {isLoading && <div className="panel"><Loading rows={6} /></div>}
      {data && results.length === 0 && data.attacks.length === 0 && (
        <div className="panel"><Empty title="No evaluation has been recorded on this installation">Run <span className="measured">python -m eval.run_eval --split dev --model &lt;model&gt;</span> in the backend to produce one. See docs/EVALUATION.md.</Empty></div>
      )}
      {!!results.length && (
        <section className="section" aria-labelledby="sets-h">
          <div className="section__head"><h2 id="sets-h">Question sets</h2><span className="label">Correct: the figure matched the reference within tolerance. Cause found: the planted cause was named. Right behaviour: clarification or refusal was the correct response.</span></div>
          {results.map((r) => (
            <div key={r.name} className="section section--tight">
              <div className="section__head">
                <h3 className="measured">{r.model}</h3>
                <span className="label">{setName(r)}, {r.n} questions, run on {day(r.at)}</span>
              </div>
              <div className="board board--wide" role="list" aria-label={`Scores for ${r.model}, ${setName(r)}`}>
                <Cell label="Figures right" name="Figures right" value={pct(r.metrics.answer_accuracy)} trend={trendOf(results, r, (x) => x.metrics.answer_accuracy)} />
                <Cell label="Cause found" name="Cause found" value={r.metrics.root_cause_rate == null ? "n/a" : pct(r.metrics.root_cause_rate)} trend={trendOf(results, r, (x) => x.metrics.root_cause_rate)} />
                <Cell label="Right behaviour" name="Right behaviour" value={r.metrics.behaviour_rate == null ? "n/a" : pct(r.metrics.behaviour_rate)} trend={trendOf(results, r, (x) => x.metrics.behaviour_rate)} />
                <Cell label="Unsupported figures" name="Unsupported figures per report" value={r.metrics.unsupported_numbers_per_report.toFixed(2)} trend={trendOf(results, r, (x) => x.metrics.unsupported_numbers_per_report)} />
                <Cell label="Steps" name="Steps per question" value={r.metrics.mean_steps.toFixed(1)} trend={trendOf(results, r, (x) => x.metrics.mean_steps)} />
                <Cell label="Cost" name="Cost per question" value={usd(r.metrics.mean_cost_usd)} trend={trendOf(results, r, (x) => x.metrics.mean_cost_usd)} />
                <Cell label="Latency" name="Latency p50 / p95" value={`${r.metrics.p50_latency_s.toFixed(0)} s / ${r.metrics.p95_latency_s.toFixed(0)} s`} trend={trendOf(results, r, (x) => x.metrics.p95_latency_s)} />
              </div>
              <div className="board board--wide" role="list" aria-label={`Questions by kind for ${r.model}, ${setName(r)}`}>
                {Object.entries(r.by_kind).map(([kind, v]) => (
                  <Cell key={kind} label={kind} name={kind.replace(/_/g, " ")} value={`${v.passed} of ${v.n}`} />
                ))}
              </div>
              {(r.note || r.regraded_at) && (
                <p className="ink-2">{r.note ?? `Graded again on ${day(r.regraded_at!)} after a grader fix; the answers are the ones first given.`}</p>
              )}
            </div>
          ))}
          {data?.unavailable?.map((u) => (
            <p key={u.name} className="ink-2"><span className="measured">{u.model}</span> was not measured: {u.note}</p>
          ))}
        </section>
      )}
      {data?.attacks.map((a) => (
        <section key={a.model + a.at} className="section" aria-label={`Attack suite, ${a.model}`}>
          <div className="section__head"><h2>Attack suite, <span className="measured">{a.model}</span></h2><span className="label">Run on {day(a.at)}.{a.note ? ` ${a.note}` : ""}</span></div>
          <div className="board board--wide" role="list" aria-label="Attack results">
            {a.cases.map((c) => {
              const result = c.result ?? (c.passed ? "held" : "failed");
              const tone = RESULT_TONE[result];
              return (
                <div key={c.id} role="listitem" className="attack">
                  <span className="board__name">{c.name}</span>
                  <span className={`board__value board__value--text ${tone === "ok" ? "board__value--ok" : tone === "bad" ? "board__value--bad" : ""}`}>{result}</span>
                  <span className="board__caption">{c.detail}</span>
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </Page>
  );
}
