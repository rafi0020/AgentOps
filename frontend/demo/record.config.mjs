// What to record for the AgentOps demo. Run with the stack up and a model connected (see README, "Adding an AI model").
const email = (role) => `${role}@lumen.demo`;
const PASSWORDS = { engineer: "Engineer#Demo2026", admin: "Admin#Demo2026", viewer: "Viewer#Demo2026" };

async function investigate(page, h, question) {
  await page.goto("/investigations/");
  await h.settle(500);
  await page.getByPlaceholder(/Why did errors rise/).fill(question);
  await page.getByRole("button", { name: "Ask the agent" }).click();
  await page.waitForURL(/run=/);
  await page.getByText("Completed", { exact: true }).first().waitFor({ timeout: 300_000 });
  await h.settle(1500);
  await h.openEvidence();
}

export default {
  project: "agentops",
  baseUrl: process.env.DEMO_BASE_URL ?? "http://localhost:3200",
  loginPath: "/login/",
  loginApi: "/api/auth/login",
  logoutApi: "/api/auth/logout",
  crawlStart: "/overview/",
  crawlMax: 45,
  crawlSkip: /[?&]run=/,
  poke: true,
  pokeRows: 3,
  sequence: [/^\/api\/investigations\/[^/]+$/],
  users: [
    { email: email("engineer"), password: PASSWORDS.engineer, label: "Engineer" },
    { email: email("admin"), password: PASSWORDS.admin, label: "Administrator" },
    { email: email("viewer"), password: PASSWORDS.viewer, label: "Viewer" },
  ],
  journeys: {
    [email("engineer")]: async (page, h) => {
      await investigate(page, h, "Something broke order lookups in checkout-assistant for about two days. What was it, and is it still happening?");
      await investigate(page, h, "Why did latency rise for the summarizer, and which model or prompt change explains it?");
    },
  },
};
