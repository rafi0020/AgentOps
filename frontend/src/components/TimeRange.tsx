"use client";

export const RANGES = [["1h", "Last hour", 1], ["24h", "Last 24 hours", 24], ["7d", "Last 7 days", 168], ["30d", "Last 30 days", 720]] as const;
export type RangeKey = (typeof RANGES)[number][0];

/** Whole minutes, so a range is stable between renders and cache keys repeat. */
export function rangeOf(key: string | null): { from: string; to: string; hours: number; key: RangeKey } {
  const found = RANGES.find((r) => r[0] === key) ?? RANGES[1];
  const to = new Date(Math.ceil(Date.now() / 60_000) * 60_000);
  const from = new Date(to.getTime() - found[2] * 3600_000);
  return { from: from.toISOString(), to: to.toISOString(), hours: found[2], key: found[0] };
}

export function stepFor(hours: number): "5m" | "1h" | "1d" {
  return hours <= 6 ? "5m" : hours <= 24 * 7 ? "1h" : "1d";
}

export function RangePicker({ value, onChange }: { value: string; onChange: (key: string) => void }) {
  return (
    <label className="field">
      <span className="visually-hidden">Time range</span>
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
        {RANGES.map(([key, label]) => (<option key={key} value={key}>{label}</option>))}
      </select>
    </label>
  );
}
