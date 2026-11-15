import { expect, test } from "@playwright/test";
import { LUMEN, signIn, signInFresh } from "./helpers";

test("a wrong password shows the server's message and does not sign in", async ({ page }) => {
  await page.goto("/login/");
  await page.getByLabel("E-mail").fill("engineer@lumen.demo");
  await page.getByLabel("Password").fill("definitely-wrong-1");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("Incorrect e-mail or password.")).toBeVisible();
  await expect(page).toHaveURL(/\/login\//);
});

test("a signed-out visitor is sent to sign-in", async ({ page }) => {
  await page.goto("/traces/");
  await expect(page).toHaveURL(/\/login\//);
});

for (const [role, hidden, visible] of [
  ["viewer", ["Approvals", "Administration"], ["Overview", "Traces", "Prompts", "Monitors", "Investigations", "Evaluation"]],
  ["engineer", ["Administration"], ["Approvals", "Experiments", "Datasets"]],
  ["admin", [], ["Approvals", "Administration"]],
] as const) {
  test(`${role} sees only the sections that role may use`, async ({ page }) => {
    await signIn(page, LUMEN[role]);
    const nav = page.getByRole("navigation", { name: "Main" });
    for (const name of visible) await expect(nav.getByRole("link", { name: new RegExp(`^${name}`) })).toBeVisible(); // a count may follow
    for (const name of hidden) await expect(nav.getByRole("link", { name, exact: true })).toHaveCount(0);
  });
}

test("signing in through the form and signing out ends that session", async ({ page }) => {
  await signInFresh(page, LUMEN.viewer);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login\//);
  await page.goto("/overview/");
  await expect(page).toHaveURL(/\/login\//);
});

test("the page has no horizontal scroll on a phone and the menu opens", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await signIn(page, LUMEN.viewer);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBe(0);
  await page.getByRole("button", { name: "Menu" }).click();
  await expect(page.getByRole("link", { name: "Traces" })).toBeVisible();
});
