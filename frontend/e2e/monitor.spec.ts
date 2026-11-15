import { expect, test } from "@playwright/test";
import { eventually, LUMEN, newProject, send, sendTracesWithPythonClient, signIn } from "./helpers";

// A failure in the traffic raises an alert, and the alert starts an investigation that ends in a report whose
// figures come from evidence. With LLM_PROVIDER=stub the investigation is the built-in script, so this checks
// the machinery (rule, roll-ups, alert, run, evidence, live events), not a model's reasoning.
// A monitor with window_minutes = 15 reads the last COMPLETE 15-minute window. Its windows sit on the UTC grid
// (:00, :15, :30, :45), and a window is decided once. So the traffic must start inside the window that has just
// finished, not in the one still open. Sending and evaluating take well under a minute, so the test may start
// at any time up to MAX_LEAD_MINUTES after a boundary; later than that, it waits for the boundary first.
const WINDOW_MS = 15 * 60_000;
const MAX_LEAD_MINUTES = 13;

/** Minutes since the last 15-minute UTC boundary. Epoch-aligned, so it matches the backend's grid. */
const minutesIntoWindow = () => (Date.now() % WINDOW_MS) / 60_000;

/** Returns how far into the current 15-minute window it is, after waiting past a boundary if it is too late in one. */
async function startInSafeWindow(): Promise<number> {
  if (minutesIntoWindow() > MAX_LEAD_MINUTES) {
    await new Promise((resolve) => setTimeout(resolve, WINDOW_MS - (Date.now() % WINDOW_MS) + 5_000));
  }
  return minutesIntoWindow();
}

test("an alert raised by failing traffic leads to an investigation with a report and evidence", async ({ page }) => {
  // Up to about two minutes of waiting for a boundary, plus the run itself.
  test.setTimeout(240_000);
  await signIn(page, LUMEN.admin);
  const project = await newProject(page);
  const pairs: [string, string][] = Array.from({ length: 25 }, (_, i) => [`question ${i}`, "sorry"]);
  // Starting floor(m) + 8 minutes ago puts every span 7 to 8 minutes before the last boundary: inside the complete window.
  const minutesAgo = Math.floor(await startInSafeWindow()) + 8;
  sendTracesWithPythonClient(project.key, "support-bot", pairs, { failed: true, minutesAgo });
  await eventually(async () => (await (await page.request.get(`/api/traces?project=${project.id}`)).json()).traces?.length === 25);

  const rule = `Errors above 10% in ${project.name}`;
  await send(page, "post", "/api/monitors/rules", {
    project_id: project.id, name: rule, metric: "error_rate", comparison: ">", threshold: 0.1, window_minutes: 15,
  });
  await send(page, "post", "/api/monitors/evaluate", {});
  await page.goto(`/monitors/?project=${project.id}`);
  const alert = page.locator("tr").filter({ hasText: rule }).filter({ has: page.getByRole("button", { name: "Investigate" }) }).first();
  await alert.getByRole("button", { name: "Investigate" }).click();

  await expect(page).toHaveURL(/\/investigations\/\?.*run=/);
  await expect(page.getByText("Completed", { exact: true }).first()).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText(/built-in demonstration script/).first()).toBeVisible();
  const runId = new URL(page.url()).searchParams.get("run");
  const run = await (await page.request.get(`/api/investigations/${runId}`)).json();
  expect(run.report.findings.length).toBeGreaterThan(0);
  expect(run.report.findings[0].evidence[0]).toMatch(/^ev_/);
});
