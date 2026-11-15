export type Role = "viewer" | "engineer" | "admin";
export interface User { id: string; org_id: string; email: string; name: string; role: Role }
export interface Step {
  idx: number; kind: "model" | "tool" | "note"; name: string; state: string; error: string | null;
  input: Record<string, unknown> | null; summary: string; handle: string | null; tool_calls: string[];
  tokens: number; cost_usd?: number; latency_ms: number;
}
export interface NumberRef { text: string; handle: string }
export interface ChartSpec { kind: "line" | "bar" | "waterfall"; title: string; x: string; labels: string[]; series: { name: string; data: (number | null)[] }[]; handle?: string; chart_handle?: string }
export interface Report {
  outcome: "answered" | "cannot_answer"; summary: string; summary_numbers: NumberRef[];
  findings: { number: number; claim: string; evidence: string[]; numbers: NumberRef[] }[];
  recommendations: string[]; charts: ChartSpec[]; unsupported: string[];
  evidence: { handle: string; kind: string; purpose: string | null; sql: string | null; row_count: number }[];
}
export type RunStatus = "queued" | "running" | "waiting_approval" | "waiting_user" | "completed" | "failed" | "cancelled" | "budget_exceeded";
export interface Run {
  id: string; question: string; status: RunStatus; error: string | null; pending: { kind?: string; question?: string; tool?: string } | null;
  steps_used: number; tokens_used: number; cost_usd: number; asked_by: string | null; user_id: string | null;
  created_at: string; finished_at: string | null; steps?: Step[]; report?: Report | null;
}
export interface Evidence { handle: string; kind: string; sql: string | null; row_count: number; payload: Record<string, unknown> & { columns?: string[]; rows?: unknown[][]; rows_omitted?: number } }
export interface Approval { id: string; run_id: string; question: string; tool: string; arguments: Record<string, unknown>; status: string; reason: string | null; requested_by: string | null; created_at: string; decided_at: string | null }

export interface Figures { calls: number; errors: number; error_rate: number | null; roots: number; input_tokens: number; output_tokens: number; cost_usd: number; p50_ms: number | null; p95_ms: number | null }
export interface TraceRow { trace_id: string; root_name: string; status: string; start_time: string; duration_ms: number; span_count: number; error_count: number; tokens: number; cost_usd: number; models: string[]; tools: string[]; session_id: string | null; service_name: string | null; service_version: string | null }
export interface SpanRow { span_id: string; parent_span_id: string | null; name: string; status: string; status_message: string | null; depth: number; offset_ms: number; duration_ms: number; operation: string | null; model: string | null; tool: string | null; provider: string | null; prompt_name: string | null; prompt_version: number | null; input_tokens: number | null; output_tokens: number | null; cost_usd: number | null; attributes: Record<string, unknown>; content: { input?: unknown; output?: unknown } | null; has_content: boolean }
export interface Score { value: number; passed: boolean; reason: string; invalid: boolean; raw: number | null }
export interface Evaluator { id: string; name: string; kind: string; config: Record<string, unknown>; calibration?: { status: string; why: string; n: number; kappa: number | null } }
export interface ExperimentRow { id: string; name: string; project_id: string; dataset_id: string; status: string; target: Record<string, unknown>; evaluator_ids: string[]; totals: Record<string, { n: number; passed: number; invalid: number; mean: number | null; pass_rate: number | null }> | null; error: string | null; items_total: number; items_done: number; cost_usd: number; created_at: string; finished_at: string | null }
export interface Release { id: string; prompt_id: string; prompt: string | null; from_version: number | null; to_version: number; experiment_id: string | null; gate: { passed: boolean; checks: { name: string; passed: boolean; detail: string }[] }; status: string; reason: string | null; requested_by: string | null; created_at: string; decided_at: string | null }
