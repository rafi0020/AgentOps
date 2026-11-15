"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";
import { useApi } from "@/lib/api";

export interface Project { id: string; name: string; retention_days: number }
const KEY = "agentops.project";

/** The current project lives in the URL (?project=), so links and reloads keep it; the last one used is remembered. */
export function useProject(): { project?: Project; projects: Project[]; isLoading: boolean } {
  const params = useSearchParams();
  const { data, isLoading } = useApi<{ projects: Project[] }>("/api/projects");
  const projects = data?.projects ?? [];
  let wanted = params.get("project");
  if (!wanted && typeof window !== "undefined") {
    try { wanted = localStorage.getItem(KEY); } catch { /* private mode */ }
  }
  const project = projects.find((p) => p.id === wanted) ?? projects[0];
  return { project, projects, isLoading };
}

export function withProject(href: string, projectId?: string): string {
  if (!projectId) return href;
  return `${href}${href.includes("?") ? "&" : "?"}project=${projectId}`;
}

function Switcher() {
  const router = useRouter();
  const path = usePathname();
  const params = useSearchParams();
  const { project, projects } = useProject();
  useEffect(() => {
    if (project) { try { localStorage.setItem(KEY, project.id); } catch { /* private mode */ } }
  }, [project]);
  if (!projects.length) return null;
  return (
    <label className="field">
      <span className="visually-hidden">Project</span>
      <select className="select project-switch" value={project?.id ?? ""}
        onChange={(e) => { const next = new URLSearchParams(params); next.set("project", e.target.value); router.replace(`${path}?${next}`); }}>
        {projects.map((p) => (<option key={p.id} value={p.id}>{p.name}</option>))}
      </select>
    </label>
  );
}

export function ProjectSwitcher() {
  return (<Suspense fallback={null}><Switcher /></Suspense>);
}
