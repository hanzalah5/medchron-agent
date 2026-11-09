import { expect, test } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SAMPLE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../data/synthetic/case_0003.pdf");
const SHOTS = process.env.SCREENSHOT_DIR;

test("upload → pipeline → click citation → highlighted source → review", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "New matter" })).toBeVisible();

  // Upload a synthetic production and run the demo engine.
  await page.locator('input[name="file"]').setInputFiles(SAMPLE);
  await page.locator('input[name="matter_id"]').fill("e2e-case-0003");
  await page.locator('input[name="title"]').fill("Synthetic case 3 (abdominal pain)");
  await page.locator('select[name="provider"]').selectOption("oracle");
  await page.getByRole("button", { name: "Upload and run" }).click();

  // The worker processes the job, and the chronology appears once it finishes.
  const rows = page.getByTestId("event-row");
  await expect(rows.first()).toBeVisible({ timeout: 60_000 });
  expect(await rows.count()).toBeGreaterThan(10);
  await expect(page.getByTestId("for-review")).toContainText("possible missing records");
  await expect(page.getByTestId("for-review")).toContainText("prompt injection");

  // Clicking a citation opens the source page and highlights the quoted text.
  const ctRow = rows.filter({ hasText: "perforated appendicitis" }).first();
  await ctRow.getByTestId("cite").first().click();
  await expect(page.getByTestId("highlight").first()).toBeVisible();
  await expect(page.getByTestId("cited-quote")).toContainText("Perforated appendicitis");
  await expect(page.getByTestId("page-label")).toContainText("Page");
  await page.locator(".page-frame img").evaluate((img: HTMLImageElement) => img.decode());
  if (SHOTS) {
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: `${SHOTS}/reviewer.png`, fullPage: false });
  }

  // Accept one event, then confirm the document index (HITL-1 and HITL-2).
  await ctRow.getByRole("button", { name: "Accept" }).click();
  await expect(ctRow).toContainText("accepted");
  await page.getByTestId("confirm-index").click();
  await expect(page.getByTestId("doc-index")).toContainText("confirmed by");

  // The review state survives a reload.
  await page.reload();
  await expect(page.getByTestId("event-row").filter({ hasText: "perforated appendicitis" }).first()).toContainText(
    "accepted",
  );

  // The export reflects reviewer decisions.
  const exported = await page.request.get("/api/matters/e2e-case-0003/export.md");
  expect(exported.ok()).toBeTruthy();
  expect(await exported.text()).toContain("review:accepted");

  if (SHOTS) {
    await page.goto("/");
    await page.screenshot({ path: `${SHOTS}/matters.png` });
  }
});
