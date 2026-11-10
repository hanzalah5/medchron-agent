import AxeBuilder from "@axe-core/playwright";
import { expect, Page, test } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SAMPLE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../data/synthetic/dev/case_0005.pdf");
const MATTER = "e2e-a11y";

/** Fail on serious or critical WCAG 2.0/2.1 A and AA violations. */
async function audit(page: Page, label: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const summary = blocking.map(
    (v) =>
      `${v.id} (${v.impact}): ${v.help} at ${v.nodes
        .slice(0, 3)
        .map((n) => `${n.target.join(" ")} [${n.failureSummary?.split("\n").slice(-1)[0] ?? ""}]`)
        .join("; ")}`,
  );
  expect(summary, `${label}: accessibility violations`).toEqual([]);
}

test("sign-in page is accessible", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await audit(page, "sign-in");
});

test("matter list and every matter tab are accessible", async ({ page, context, baseURL }) => {
  await context.addCookies([{ name: "medchron_dev", value: "e2e-a11y:attorney", url: baseURL! }]);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "New matter" })).toBeVisible();
  await audit(page, "matter list");

  await page.locator('input[name="file"]').setInputFiles(SAMPLE);
  await page.locator('input[name="matter_id"]').fill(MATTER);
  await page.locator('select[name="provider"]').selectOption("oracle");
  await page.getByRole("button", { name: "Upload and run" }).click();
  await expect(page.getByTestId("event-row").first()).toBeVisible({ timeout: 60_000 });
  await audit(page, "review tab");
  for (const [tab, testid] of [
    ["providers", "provider-row"],
    ["gaps", "gaps"],
    ["versions", "versions"],
    ["export", "export-panel"],
  ] as const) {
    await page.getByTestId(`tab-${tab}`).click();
    await expect(page.getByTestId(testid).first()).toBeVisible();
    await audit(page, `${tab} tab`);
  }
});
