"use client";

import { Fragment, useState } from "react";
import { ms, usd } from "@/lib/format";
import type { SpanRow } from "@/lib/types";

/** Every span of a trace on one time axis: where the time went, and what failed. The trace fills the width. */
export function TraceTimeline({ spans, total, canSeeContent }: { spans: SpanRow[]; total: number; canSeeContent: boolean }) {
  const [selected, setSelected] = useState(spans.find((s) => s.status === "error")?.span_id ?? spans[0]?.span_id);
  const span = spans.find((s) => s.span_id === selected);
  const scale = Math.max(total, 1);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => ms(total * f));
  return (
    <div className="grid gap-4 min-w-0">
      <section className="panel">
        <div className="panel__head"><h2>Timeline</h2><span className="label measured">{spans.length} spans, {ms(total)}</span></div>
        <div className="panel__body">
          <div className="timeline" role="listbox" aria-label="Spans">
            <div className="timeline__axis" aria-hidden>
              <span>span</span>
              <span className="timeline__ticks">{ticks.map((t) => <span key={t}>{t}</span>)}</span>
            </div>
            {spans.map((s) => (
              <button key={s.span_id} className="timeline__row" role="option" aria-selected={s.span_id === selected} onClick={() => setSelected(s.span_id)}>
                <span className="timeline__name" style={{ paddingLeft: `${Math.min(s.depth, 8) * 12}px` }}>
                  {s.status === "error" && <span className="bad">failed </span>}{s.name}
                </span>
                <span className="timeline__track" aria-hidden>
                  <span className="timeline__bar" data-status={s.status} data-kind={s.operation ?? ""}
                    style={{ left: `${(s.offset_ms / scale) * 100}%`, width: `${Math.max((s.duration_ms / scale) * 100, 0.4)}%` }} />
                </span>
              </button>
            ))}
          </div>
        </div>
      </section>
      {span && (
        <section className="panel" aria-label="Selected span">
          <div className="panel__head"><h2 className="truncate">{span.name}</h2><span className={span.status === "error" ? "chip chip--bad" : "chip"}>{span.status}</span></div>
          <div className="panel__body grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)] items-start">
            <div className="grid gap-3 min-w-0">
              {span.status_message && <div className="banner banner--bad">{span.status_message}</div>}
              <dl className="kv">
                <dt>Starts at</dt><dd>+{ms(span.offset_ms)}</dd>
                <dt>Duration</dt><dd>{ms(span.duration_ms)}</dd>
                {span.model && (<><dt>Model</dt><dd>{span.model}{span.provider ? ` (${span.provider})` : ""}</dd></>)}
                {span.tool && (<><dt>Tool</dt><dd>{span.tool}</dd></>)}
                {span.prompt_name && (<><dt>Prompt</dt><dd>{span.prompt_name} v{span.prompt_version}</dd></>)}
                {span.input_tokens != null && (<><dt>Tokens</dt><dd>{span.input_tokens} in, {span.output_tokens} out</dd></>)}
                {span.cost_usd != null && (<><dt>Estimated cost</dt><dd>{usd(span.cost_usd)}</dd></>)}
                {Object.entries(span.attributes).slice(0, 30).map(([k, v]) => (<Fragment key={k}><dt>{k}</dt><dd>{typeof v === "string" ? v : JSON.stringify(v)}</dd></Fragment>))}
              </dl>
              {span.has_content && !canSeeContent && <p className="muted">This span carries prompt and output content, which viewers cannot see.</p>}
            </div>
            {span.content && (
              <div className="grid gap-2 min-w-0">
                {span.content.input !== undefined && (<><div className="label">Input</div><div className="well code">{typeof span.content.input === "string" ? span.content.input : JSON.stringify(span.content.input, null, 2)}</div></>)}
                {span.content.output !== undefined && (<><div className="label">Output</div><div className="well code">{typeof span.content.output === "string" ? span.content.output : JSON.stringify(span.content.output, null, 2)}</div></>)}
              </div>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
