import type { Release } from "@/lib/types";

/** Each check of a release request, passed or failed, with the reason recorded at the time. */
export function GateChecks({ release }: { release: Release }) {
  return (
    <ul className="grid gap-1 m-0 p-0 list-none">
      {release.gate.checks.map((c) => (
        <li key={c.name} className="check"><span className={c.passed ? "ok" : "bad"}>{c.passed ? "passed" : "failed"}</span><span><strong>{c.name}</strong>: <span className="ink-2">{c.detail}</span></span></li>
      ))}
    </ul>
  );
}
