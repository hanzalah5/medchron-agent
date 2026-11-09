import { expect, test } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Captures frames for the README demo GIF. Runs only when GIF_DIR is set:
//   GIF_DIR=../docs/images/frames E2E_DATABASE_URL=... npx playwright test demo.spec.ts
const DIR = process.env.GIF_DIR;
const SAMPLE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../data/synthetic/case_0003.pdf");

test.skip(!DIR, "set GIF_DIR to record demo frames");
test.use({ viewport: { width: 1280, height: 760 }, deviceScaleFactor: 1 });

test("demo frames", async ({ page, context, baseURL }) => {
  let n = 0;
  const shot = async () => {
    await page.waitForTimeout(250);
    await page.screenshot({ path: `${DIR}/${String(++n).padStart(2, "0")}.png` });
  };
  await context.addCookies([{ name: "medchron_dev", value: "demo-attorney:attorney", url: baseURL! }]);
  page.on("dialog", (d) => d.accept("Injection text, noted for the file."));

  await page.goto("/");
  await page.locator('input[name="file"]').setInputFiles(SAMPLE);
  await page.locator('input[name="matter_id"]').fill("demo-case-3");
  await page.locator('input[name="title"]').fill("Synthetic case 3: abdominal pain, return visit");
  await page.locator('select[name="provider"]').selectOption("oracle");
  await shot();
  await page.getByRole("button", { name: "Upload and run" }).click();

  const rows = page.getByTestId("event-row");
  await expect(rows.first()).toBeVisible({ timeout: 60_000 });
  await shot();

  // Click to source: the cited CT impression is highlighted on the page.
  const ct = rows.filter({ hasText: "perforated appendicitis" }).first();
  await ct.scrollIntoViewIfNeeded();
  await ct.getByTestId("cite").first().click();
  await page.locator(".page-frame img").evaluate((img: HTMLImageElement) => img.decode());
  await shot();

  // The WBC event has two sources, one on a scanned (OCR) page.
  const wbc = rows.filter({ hasText: "White blood cell" }).first();
  await wbc.scrollIntoViewIfNeeded();
  await wbc.getByTestId("cite").last().click();
  await page.locator(".page-frame img").evaluate((img: HTMLImageElement) => img.decode());
  await shot();
  await ct.getByRole("button", { name: "Accept" }).click();

  // Questions for expert review: verifier verdicts, then triage.
  const issues = page.getByTestId("issue");
  await page.getByTestId("issues").scrollIntoViewIfNeeded();
  await issues.nth(1).getByRole("button").filter({ hasText: /^p\d+$/ }).first().click();
  await shot();
  const count = await issues.count();
  for (let k = 0; k < count; k++) {
    const issue = issues.nth(k);
    const injection = (await issue.textContent())?.includes("prompt injection");
    await issue.getByRole("button", { name: injection ? "Dismiss" : "Ask expert" }).click();
  }
  await page.getByTestId("confirm-index").click();
  await shot();

  // A cited memo, then attorney sign-off.
  await page.getByTestId("draft-memo").click();
  const memo = page.getByTestId("memo-body");
  await expect(memo).toContainText("DRAFT", { timeout: 60_000 });
  await page.getByTestId("memo").scrollIntoViewIfNeeded();
  await memo.locator(".cite").filter({ hasText: "p8" }).first().click();
  await page.locator(".page-frame img").evaluate((img: HTMLImageElement) => img.decode());
  await shot();
  await page.getByTestId("approve-memo").click();
  await expect(page.getByTestId("memo")).toContainText("Approved by");
  await shot();
});
