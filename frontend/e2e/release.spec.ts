import { expect, test } from "@playwright/test";
import { eventually, LUMEN, newProject, send, signIn } from "./helpers";

// A prompt goes live only through its gate and a second person: an engineer's request without an experiment
// is blocked, the same request backed by a passing experiment waits for an administrator, the administrator
// approves it on the Approvals screen, and the application then reads the new text with a scoped key.
test("a release is blocked by its gate, passes with an experiment, is approved by an administrator and read with a key", async ({ browser }) => {
  test.setTimeout(120_000);
  const admin = await (await browser.newContext()).newPage();
  await signIn(admin, LUMEN.admin);
  const project = await newProject(admin, ["ingest", "prompts:read"]);

  const engineer = await (await browser.newContext()).newPage();
  await signIn(engineer, LUMEN.engineer);
  const name = `greeting-${project.name.slice(4)}`;
  const prompt = await send<{ id: string }>(engineer, "post", "/api/prompts", { project_id: project.id, name, text: "Greet the customer: {{input}}" });
  const evaluator = await send<{ id: string }>(engineer, "post", "/api/evaluators", { project_id: project.id, name: "Fast enough", kind: "latency_under", config: { ms: 30000 } });
  const dataset = await send<{ id: string }>(engineer, "post", "/api/datasets", { project_id: project.id, name: "Greetings" });
  // The gate needs the primary evaluator to have graded at least GATE_MIN_GRADED (20) items.
  await send(engineer, "post", `/api/datasets/${dataset.id}/items`, Array.from({ length: 20 }, (_, i) => ({ input: `Guest ${i + 1}` })));
  const gate = await admin.request.patch(`/api/prompts/id/${prompt.id}/gate`, {
    data: { primary_evaluator_id: evaluator.id, min_score: 0.9 },
    headers: { "X-CSRF-Token": (await (await admin.request.get("/api/auth/me")).json()).csrf_token },
  });
  expect(gate.ok()).toBeTruthy();

  // Without an experiment the gate blocks the request, and the prompt page says why.
  const blocked = await send<{ status: string }>(engineer, "post", `/api/prompts/id/${prompt.id}/releases`, { to_version: 1 });
  expect(blocked.status).toBe("blocked");
  await engineer.goto(`/prompts/?project=${project.id}&prompt=${prompt.id}`);
  await expect(engineer.getByText(/blocked/i).first()).toBeVisible();

  // Run the dataset through version 1, then ask again with that experiment.
  const experiment = await send<{ id: string }>(engineer, "post", "/api/experiments", {
    project_id: project.id, dataset_id: dataset.id, name: "v1 check", evaluator_ids: [evaluator.id], target: { kind: "prompt", prompt_id: prompt.id, version: 1 },
  });
  await eventually(async () => (await (await engineer.request.get(`/api/experiments/${experiment.id}`)).json()).status === "completed", 60);
  const pending = await send<{ id: string; status: string }>(engineer, "post", `/api/prompts/id/${prompt.id}/releases`, { to_version: 1, experiment_id: experiment.id });
  expect(pending.status).toBe("pending");
  expect((await (await engineer.request.post(`/api/releases/${pending.id}/decide`, { data: { decision: "approved" } })).status())).toBe(403);

  // The administrator approves it on the Approvals screen.
  await admin.goto("/approvals/");
  await expect(admin.getByRole("heading", { name: "Prompt releases waiting" })).toBeVisible();
  const row = admin.locator("article, li, tr, .panel").filter({ hasText: name }).first();
  await row.getByRole("button", { name: "Approve and release" }).click();
  await eventually(async () => (await (await admin.request.get(`/api/prompts/id/${prompt.id}`)).json()).production_version === 1);

  // The application reads the live text with its key.
  const live = await admin.request.get(`/v1/prompts/${name}`, { headers: { Authorization: `Bearer ${project.key}` } });
  expect(live.status()).toBe(200);
  expect((await live.json()).text).toBe("Greet the customer: {{input}}");
});
