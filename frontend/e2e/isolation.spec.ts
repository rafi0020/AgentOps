import { expect, test } from "@playwright/test";
import { eventually, LUMEN, newProject, ORBIT_ENGINEER, sendTracesWithPythonClient, signIn } from "./helpers";

// A trace of one organisation must be invisible to another, whatever URL or API call is used. The trace is sent
// now by the test itself, so the check does not depend on the date the demo data was generated.
test("a trace cannot be opened from another organisation, by URL or by API", async ({ browser }) => {
  test.setTimeout(120_000);
  const admin = await (await browser.newContext()).newPage();
  await signIn(admin, LUMEN.admin);
  const own = await newProject(admin, ["ingest"]);
  sendTracesWithPythonClient(own.key, "isolation-check", [["Where is my order?", "Shipped"]]);

  const lumen = await (await browser.newContext()).newPage();
  await signIn(lumen, LUMEN.engineer);
  const from = new Date(Date.now() - 3_600_000).toISOString();
  const to = new Date(Date.now() + 3_600_000).toISOString();
  const trace = await eventually(async () => {
    const { traces } = await (await lumen.request.get(`/api/traces?project=${own.id}&from=${from}&to=${to}`)).json();
    return traces[0]?.trace_id as string | undefined;
  }, 60);

  const orbit = await (await browser.newContext()).newPage();
  await signIn(orbit, ORBIT_ENGINEER);
  for (const path of [`/api/traces/${own.id}/${trace}`, `/api/traces?project=${own.id}`, `/api/metrics/overview?project=${own.id}`]) {
    expect((await orbit.request.get(path)).status(), path).toBe(404);
  }
  await orbit.goto(`/traces/?project=${own.id}&trace=${trace}`);
  await expect(orbit.getByRole("heading", { name: own.name })).toHaveCount(0);
  const { projects } = await (await orbit.request.get("/api/projects")).json();
  expect(projects.map((p: { name: string }) => p.name)).not.toContain(own.name);
});
