import { expect, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

// The stack runs with LLM_PROVIDER=stub by default, so investigations use the built-in script: these journeys
// exercise the whole system (API, workers, database, live events, UI) without calling a model.
export const LUMEN = {
  admin: ["admin@lumen.demo", "Admin#Demo2026"],
  engineer: ["engineer@lumen.demo", "Engineer#Demo2026"],
  viewer: ["viewer@lumen.demo", "Viewer#Demo2026"],
} as const;
export const ORBIT_ENGINEER = ["engineer@orbit.demo", "Engineer#Demo2026"] as const;
export const ACCOUNTS = [LUMEN.admin, LUMEN.engineer, LUMEN.viewer, ORBIT_ENGINEER];
export const AUTH_DIR = "e2e/.auth";
export const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3200";

/** Open the app as this account, using the session created once in global setup. */
export async function signIn(page: Page, [email]: readonly [string, string]) {
  const state = JSON.parse(readFileSync(`${AUTH_DIR}/${email}.json`, "utf8"));
  await page.context().addCookies(state.cookies);
  await page.goto("/overview/");
  await expect(page).toHaveURL(/\/overview\//);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
}

export async function signInFresh(page: Page, [email, password]: readonly [string, string]) {
  await page.goto("/login/");
  await page.getByLabel("E-mail").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/overview\//);
}

export async function csrfHeaders(page: Page) {
  const me = await (await page.request.get("/api/auth/me")).json();
  return { "X-CSRF-Token": me.csrf_token as string };
}

/** POST/PUT as the signed-in user, failing the test with the server's message on an error. */
export async function send<T = Record<string, unknown>>(page: Page, method: "post" | "put", path: string, data: unknown): Promise<T> {
  const response = await page.request[method](path, { data, headers: await csrfHeaders(page) });
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${response.status()} ${await response.text()}`).toBeTruthy();
  return (await response.json()) as T;
}

export async function projectId(page: Page, name: string): Promise<string> {
  const { projects } = await (await page.request.get("/api/projects")).json();
  return projects.find((p: { name: string }) => p.name === name).id;
}

/** A new project with an ingestion key (admin only). */
export async function newProject(page: Page, scopes = ["ingest"]): Promise<{ id: string; key: string; name: string }> {
  const name = `e2e-${Date.now().toString(36)}`;
  const project = await send<{ id: string }>(page, "post", "/api/projects", { name });
  const key = await send<{ key: string }>(page, "post", `/api/projects/${project.id}/keys`, { name: "e2e", scopes });
  return { id: project.id, key: key.key, name };
}

/**
 * Send traces with the Python client in backend/client, as an application would: one root span per entry,
 * with a child model span carrying the captured input and output.
 */
export function sendTracesWithPythonClient(key: string, rootName: string, pairs: [string, string][], { failed = false, minutesAgo = 0 } = {}) {
  const script = `
import json, os
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from client.agentops_client import AgentOps
tracer = AgentOps(os.environ["AO_URL"], os.environ["AO_KEY"]).init_tracing("e2e-app", "1.0")
import time
t0 = time.time_ns() - int(float(os.environ["AO_AGO"]) * 60e9)  # spans may be dated in the past, e.g. for monitors
for question, answer in json.loads(os.environ["AO_PAIRS"]):
    with tracer.start_as_current_span(os.environ["AO_NAME"], start_time=t0, end_on_exit=False) as root:
        with tracer.start_as_current_span("chat model-fast", start_time=t0 + 1_000_000, end_on_exit=False) as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.request.model", "model-fast")
            span.set_attribute("gen_ai.usage.input_tokens", 42)
            span.set_attribute("gen_ai.usage.output_tokens", 7)
            span.set_attribute("input.value", question)
            span.set_attribute("output.value", answer)
            if os.environ.get("AO_FAILED") == "1":
                span.set_status(Status(StatusCode.ERROR, "provider returned 503"))
            span.end(end_time=t0 + 300_000_000)
        root.end(end_time=t0 + 320_000_000)
trace.get_tracer_provider().shutdown()
`;
  execFileSync("uv", ["run", "--quiet", "python", "-c", script], {
    cwd: "../backend",
    env: { ...process.env, AO_URL: BASE, AO_KEY: key, AO_NAME: rootName, AO_PAIRS: JSON.stringify(pairs), AO_FAILED: failed ? "1" : "0", AO_AGO: String(minutesAgo) },
    stdio: "pipe",
  });
}

/** Poll until `check` returns something truthy (the ingest worker writes a batch shortly after it is acknowledged). */
export async function eventually<T>(check: () => Promise<T | null | undefined | false>, seconds = 30): Promise<T> {
  const until = Date.now() + seconds * 1000;
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() > until) throw new Error("condition not met in time");
    await new Promise((r) => setTimeout(r, 500));
  }
}
