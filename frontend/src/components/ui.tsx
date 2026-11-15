"use client";

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import type { ApiError } from "@/lib/api";

/** A skeleton in the shape of what is coming. */
export function Loading({ rows = 4, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div className="grid gap-3 p-4" role="status" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ height: 14, width: `${88 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}

/** What belongs here, and the one action that fills it. */
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children}
    </div>
  );
}

/** What broke, and what to do. */
export function ErrorState({ error, retry }: { error: ApiError | Error; retry?: () => void }) {
  return (
    <div className="banner banner--bad" role="alert">
      <span>{error.message}</span>
      {retry && (
        <button className="btn btn--sm" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}

type Toast = { id: number; text: string; kind: "ok" | "bad" };
const ToastContext = createContext<(text: string, kind?: "ok" | "bad") => void>(() => {});
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: "ok" | "bad" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className="toast" data-kind={t.kind} role={t.kind === "bad" ? "alert" : "status"}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

const CHIP: Record<string, string> = {
  completed: "chip--ok", approved: "chip--ok", active: "chip--ok", failed: "chip--bad", rejected: "chip--bad",
  budget_exceeded: "chip--bad", waiting_user: "chip--person", waiting_approval: "chip--person", pending: "chip--person",
  running: "chip--instrument", queued: "chip--instrument",
};
export function StatusChip({ status, label }: { status: string; label?: string }) {
  return <span className={`chip ${CHIP[status] ?? ""}`}>{label ?? status.replace(/_/g, " ")}</span>;
}

export function Drawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <>
      <button className="drawer-scrim" aria-label="Close" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={title} onKeyDown={(e) => e.key === "Escape" && onClose()}>
        <div className="panel__head">
          <h2>{title}</h2>
          <button className="btn btn--sm" onClick={onClose} autoFocus>
            Close
          </button>
        </div>
        <div className="drawer__body">{children}</div>
      </aside>
    </>
  );
}

/** A sparkline of one series over time; gaps break the line. */
export function Spark({ values, label }: { values: (number | null)[]; label: string }) {
  const known = values.filter((v): v is number => v != null);
  if (known.length < 2) return <svg className="spark" role="img" aria-label={`${label}: not enough points yet`} viewBox="0 0 100 26" preserveAspectRatio="none"><line x1="0" x2="100" y1="25.5" y2="25.5" /></svg>;
  const lo = Math.min(...known), span = Math.max(...known) - lo || 1;
  const step = 100 / Math.max(values.length - 1, 1);
  const d = values.map((v, i) => (v == null ? "" : `${i === 0 || values[i - 1] == null ? "M" : "L"}${(i * step).toFixed(2)},${(24 - ((v - lo) / span) * 22 + 1).toFixed(2)}`)).join(" ");
  return (
    <svg className="spark" role="img" aria-label={label} viewBox="0 0 100 26" preserveAspectRatio="none">
      <line x1="0" x2="100" y1="25.5" y2="25.5" />
      <path d={d} />
    </svg>
  );
}

/** Two periods on one scale: the current value, and a tick where the previous period stood. */
export function Meter({ now, before }: { now: number | null | undefined; before: number | null | undefined }) {
  const scale = Math.max(now ?? 0, before ?? 0, 1e-9);
  return (
    <div className="meter" aria-hidden>
      <span className="meter__now" style={{ width: `${Math.min(((now ?? 0) / scale) * 100, 100)}%` }} />
      {before != null && <span className="meter__before" style={{ left: `${Math.min((before / scale) * 100, 100)}%` }} />}
    </div>
  );
}
