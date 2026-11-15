import { expect, test } from "@playwright/test";
import { createServer, type Server } from "node:http";
import { eventually, LUMEN, newProject, send, sendTracesWithPythonClient, signIn } from "./helpers";

// From real traffic to a regression found: an application sends traces with the Python client, the traces
// become a dataset, the dataset runs against two builds of the application, and the comparison names the
// items the second build broke. The "application" is a small HTTP server started by the test; the worker
// container reaches it at host.docker.internal.
const ANSWERS: Record<string, string> = { "Where is order 1001?": "Shipped", "Can I return shoes?": "Yes, within 30 days", "Do you ship to Oslo?": "Yes" };
const PORT = 3291;
let server: Server;

test.beforeAll(async () => {
  server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const question = (JSON.parse(body || "{}").question ?? "") as string;
      const broken = req.url?.startsWith("/v2");
      const answer = broken && question.includes("return") ? "I cannot help with that" : ANSWERS[question] ?? "";
      res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ answer }));
    });
  });
  await new Promise<void>((resolve) => server.listen(PORT, "0.0.0.0", resolve));
});
test.afterAll(() => server.close());

test("traces sent by an application become a dataset, and an experiment comparison finds the regression", async ({ page }) => {
  test.setTimeout(120_000);
  await signIn(page, LUMEN.admin);
  const project = await newProject(page);
  const root = `support-bot ${project.name}`;
  sendTracesWithPythonClient(project.key, root, Object.entries(ANSWERS));

  // The traces arrive and one opens with its spans.
  const traces = await eventually(async () => {
    const r = await (await page.request.get(`/api/traces?project=${project.id}`)).json();
    return r.traces?.length === 3 ? r.traces : null;
  });
  await page.goto(`/traces/?project=${project.id}&trace=${traces[0].trace_id}`);
  await expect(page.getByRole("heading", { name: root, level: 1 })).toBeVisible();
  await expect(page.getByText("chat model-fast").first()).toBeVisible();

  // Real traffic becomes test cases: input from the first captured input, expected from the last output.
  const dataset = await send<{ id: string }>(page, "post", "/api/datasets", { project_id: project.id, name: "From traffic" });
  const added = await send<{ added: number }>(page, "post", `/api/datasets/${dataset.id}/items/from-traces`, { trace_ids: traces.map((t: { trace_id: string }) => t.trace_id) });
  expect(added.added).toBe(3);
  await page.goto(`/datasets/?project=${project.id}&dataset=${dataset.id}`);
  await expect(page.getByText("Where is order 1001?")).toBeVisible();

  // Two builds of the application, scored by exact match against the recorded answers.
  const evaluator = await send<{ id: string }>(page, "post", "/api/evaluators", { project_id: project.id, name: "Same answer", kind: "exact", config: {} });
  const run = async (name: string, path: string) =>
    send<{ id: string }>(page, "post", "/api/experiments", {
      project_id: project.id, dataset_id: dataset.id, name, evaluator_ids: [evaluator.id],
      target: { kind: "http", url: `http://host.docker.internal:${PORT}${path}`, body_template: { question: "{{input}}" }, output_path: "answer" },
    });
  const [v1, v2] = [await run("Build 1", "/v1"), await run("Build 2", "/v2")];
  for (const e of [v1, v2]) {
    await eventually(async () => (await (await page.request.get(`/api/experiments/${e.id}`)).json()).status === "completed", 60);
  }
  const comparison = await (await page.request.get(`/api/experiments/compare?a=${v1.id}&b=${v2.id}`)).json();
  expect(comparison.per_evaluator[evaluator.id].mean_a).toBe(1);
  expect(comparison.per_evaluator[evaluator.id].delta).toBeLessThan(0);
  expect(comparison.regressions).toBe(1);
  expect(comparison.items[0].change).toBe("regression"); // what got worse is listed first

  await page.goto(`/experiments/?project=${project.id}&a=${v1.id}&b=${v2.id}`);
  await expect(page.getByRole("heading", { name: "Build 1 against Build 2", level: 1 })).toBeVisible();
  await expect(page.getByText("1 item got worse and 0 got better.", { exact: false })).toBeVisible();
  await expect(page.getByRole("cell", { name: "worse", exact: true })).toHaveCount(1);
});
