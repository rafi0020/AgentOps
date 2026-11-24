"use client";

import { Bell, ClipboardCheck, Database, FileText, FlaskConical, LayoutDashboard, LogOut, Menu, Moon, Rows3, Search, Settings, ShieldCheck, Sun, Waypoints } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { BASE, api, useApi, useMe } from "@/lib/api";
import type { Role } from "@/lib/types";
import { useQueryState } from "@/lib/url";
import { ProjectSwitcher, useProject, withProject } from "./Project";
import { RANGES, RangePicker } from "./TimeRange";
import { Loading, ToastProvider } from "./ui";

const NAV: { href: string; label: string; icon: typeof Bell; roles: Role[]; count?: "alerts" | "approvals" }[] = [
  { href: "/overview/", label: "Overview", icon: LayoutDashboard, roles: ["viewer", "engineer", "admin"] },
  { href: "/traces/", label: "Traces", icon: Waypoints, roles: ["viewer", "engineer", "admin"] },
  { href: "/sessions/", label: "Sessions", icon: Rows3, roles: ["viewer", "engineer", "admin"] },
  { href: "/datasets/", label: "Datasets", icon: Database, roles: ["viewer", "engineer", "admin"] },
  { href: "/experiments/", label: "Experiments", icon: FlaskConical, roles: ["viewer", "engineer", "admin"] },
  { href: "/prompts/", label: "Prompts", icon: FileText, roles: ["viewer", "engineer", "admin"] },
  { href: "/monitors/", label: "Monitors", icon: Bell, roles: ["viewer", "engineer", "admin"], count: "alerts" },
  { href: "/investigations/", label: "Investigations", icon: Search, roles: ["viewer", "engineer", "admin"] },
  { href: "/approvals/", label: "Approvals", icon: ClipboardCheck, roles: ["engineer", "admin"], count: "approvals" },
  { href: "/evaluation/", label: "Evaluation", icon: ShieldCheck, roles: ["viewer", "engineer", "admin"] },
  { href: "/admin/", label: "Administration", icon: Settings, roles: ["admin"] },
];

/** Pages whose figures are read over a time range; the range picker lives in the command bar for them. */
const RANGED = ["/overview/", "/traces/", "/sessions/"];

function useTheme() {
  const [theme, setTheme] = useState("dark");
  useEffect(() => setTheme(document.documentElement.dataset.theme ?? "dark"), []);
  return [theme, () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch { /* private mode */ }
    setTheme(next);
  }] as const;
}

export function Shell({ children }: { children: ReactNode }) {
  const { user, signedOut, isLoading } = useMe();
  const router = useRouter();
  const path = usePathname();
  const { params, set } = useQueryState();
  const [open, setOpen] = useState(false);
  const [theme, toggleTheme] = useTheme();
  const { project } = useProject();
  const canAct = user && user.role !== "viewer";
  const alerts = useApi<{ unacknowledged: number }>(user ? "/api/monitors/alerts?open_only=true&limit=1" : null, { refreshInterval: 60_000 });
  const approvals = useApi<{ approvals: unknown[] }>(canAct ? "/api/approvals?status=pending" : null, { refreshInterval: 30_000 });
  const releases = useApi<{ releases: unknown[] }>(user?.role === "admin" ? "/api/releases?status=pending" : null, { refreshInterval: 30_000 });
  const counts = { alerts: alerts.data?.unacknowledged ?? 0, approvals: (approvals.data?.approvals.length ?? 0) + (releases.data?.releases.length ?? 0) };

  useEffect(() => { if (signedOut) router.replace("/login/"); }, [signedOut, router]);
  useEffect(() => setOpen(false), [path]);

  if (isLoading || !user) return <Loading rows={6} label="Signing in" />;
  const signOut = async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    window.location.href = `${BASE}/login/`;
  };
  const current = NAV.find((n) => path.startsWith(n.href.slice(0, -1)));
  const ranged = RANGED.some((p) => path.startsWith(p)) && !params.get("trace");
  const rangeKey = params.get("range") ?? (path.startsWith("/sessions") ? "7d" : "24h");
  const knownRange = RANGES.some((r) => r[0] === rangeKey) ? rangeKey : "24h";

  return (
    <ToastProvider>
      <div className="console">
        <header className="cmdbar">
          <button className="btn btn--quiet btn--sm cmdbar__menu" onClick={() => setOpen(!open)} aria-expanded={open} aria-controls="rail">
            <Menu aria-hidden />Menu
          </button>
          <span className="cmdbar__brand"><span className="cmdbar__mark" aria-hidden />AgentOps Control Plane</span>
          <div className="cmdbar__tools">
            <ProjectSwitcher />
            {ranged && <RangePicker value={knownRange} onChange={(key) => set({ range: key })} />}
          </div>
          {current && <span className="cmdbar__crumb">{current.label.toLowerCase()}</span>}
          <span className="cmdbar__who"><span>{user.email}</span><span className="chip">{user.role}</span></span>
        </header>
        {open && <button className="scrim" aria-label="Close menu" onClick={() => setOpen(false)} />}
        <aside className="rail" id="rail" data-open={open}>
          <nav aria-label="Main" className="rail__nav">
            {NAV.filter((n) => n.roles.includes(user.role)).map(({ href, label, icon: Icon, count }) => {
              const n = count ? counts[count] : 0;
              return (
                <Link key={href} href={withProject(href, project?.id)} className="rail__link"
                  aria-label={n > 0 ? `${label}, ${n} waiting` : label}
                  aria-current={path.startsWith(href.slice(0, -1)) ? "page" : undefined}>
                  <Icon aria-hidden />
                  <span className="rail__label" aria-hidden>{label}</span>
                  {n > 0 && <span className="rail__badge" aria-hidden>{n}</span>}
                  <span className="rail__tip" aria-hidden>{label}</span>
                </Link>
              );
            })}
          </nav>
          <div className="rail__foot">
            <button className="rail__link" onClick={toggleTheme} aria-label={theme === "dark" ? "Light theme" : "Dark theme"}>
              {theme === "dark" ? <Sun aria-hidden /> : <Moon aria-hidden />}
              <span className="rail__label" aria-hidden>{theme === "dark" ? "Light theme" : "Dark theme"}</span>
              <span className="rail__tip" aria-hidden>{theme === "dark" ? "Light theme" : "Dark theme"}</span>
            </button>
            <button className="rail__link" onClick={signOut} aria-label="Sign out">
              <LogOut aria-hidden />
              <span className="rail__label" aria-hidden>Sign out</span>
              <span className="rail__tip" aria-hidden>Sign out</span>
            </button>
          </div>
        </aside>
        <main className="main">{children}</main>
      </div>
    </ToastProvider>
  );
}

export function Page({ title, lede, actions, children }: { title: string; lede?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="page">
      <header className="page__head">
        <div>
          <h1>{title}</h1>
          {lede && <p>{lede}</p>}
        </div>
        {actions && <div>{actions}</div>}
      </header>
      {children}
    </div>
  );
}
