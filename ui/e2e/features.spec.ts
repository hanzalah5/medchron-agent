import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEV = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../data/synthetic/dev");
const manifest = JSON.parse(fs.readFileSync(path.join(DEV, "manifest.json"), "utf-8")) as {
  case_id: string;
  productions: number;
  pdfs: string[];
}[];
const twoProductions = manifest.find((m) => m.productions === 2)!;
const [P1, P2] = twoProductions.pdfs.map((f) => path.join(DEV, f));
const MATTER = "e2e-incremental";

test("second production → version diff, providers, gaps, gated export", async ({ page, context, baseURL }) => {
  await context.addCookies([{ name: "medchron_dev", value: "e2e-attorney:attorney", url: baseURL! }]);
  await page.goto("/");

  // Production 1.
  await page.locator('input[name="file"]').setInputFiles(P1);
  await page.locator('input[name="matter_id"]').fill(MATTER);
  await page.locator('input[name="title"]').fill("Synthetic case delivered in two productions");
  await page.locator('select[name="provider"]').selectOption("oracle");
  await page.getByRole("button", { name: "Upload and run" }).click();
  await expect(page.getByTestId("event-row").first()).toBeVisible({ timeout: 60_000 });
  const firstCount = await page.getByTestId("event-row").count();

  // Bates IDs are shown with each page.
  await page.getByTestId("cite").first().click();
  await expect(page.getByTestId("page-label")).toContainText("E2EINCREMENT-0000");

  // Production 2 from the Versions tab: a new version with a diff.
  await page.getByTestId("tab-versions").click();
  await expect(page.getByTestId("productions")).toContainText("p1–");
  await page.getByTestId("productions").locator('input[type="file"]').setInputFiles(P2);
  await page.getByTestId("add-production").click();
  await expect(page.getByTestId("versions")).toContainText("v2", { timeout: 60_000 });
  await expect(page.getByTestId("diff")).toContainText("v1 → v2");
  await expect(page.getByTestId("diff-added").first()).toBeVisible();
  await expect(page.getByTestId("diff-removed")).toHaveCount(0);
  await expect(page.getByTestId("productions").locator("tbody tr")).toHaveCount(2);
  await page.getByTestId("tab-review").click();
  expect(await page.getByTestId("event-row").count()).toBeGreaterThan(firstCount);

  // Provider and facility index.
  await page.getByTestId("tab-providers").click();
  await expect(page.getByTestId("provider-row").first()).toBeVisible();
  await expect(page.getByTestId("providers")).toContainText("E2EINCREMENT-");

  // Gaps in treatment, with an adjustable threshold.
  await page.getByTestId("tab-gaps").click();
  await page.getByTestId("gap-days").fill("1");
  await expect(page.getByTestId("gap-interval").first()).toBeVisible();

  // Export is refused until an attorney approves the confirmed chronology.
  const refused = await page.request.get(`/api/matters/${MATTER}/chronology/export.docx`);
  expect(refused.status()).toBe(409);
  await page.getByTestId("tab-export").click();
  await expect(page.getByTestId("approve-chronology")).toBeDisabled();
  await page.getByTestId("tab-review").click();
  await page.getByTestId("confirm-index").click();
  await page.getByTestId("tab-export").click();
  await page.getByTestId("approve-chronology").click();
  await expect(page.getByTestId("approval-status")).toContainText("approved by e2e-attorney");

  const download = page.waitForEvent("download");
  await page.getByTestId("export-docx").click();
  expect((await download).suggestedFilename()).toBe(`${MATTER}-chronology.docx`);
  const xlsx = await page.request.get(`/api/matters/${MATTER}/chronology/export.xlsx`);
  expect(xlsx.ok()).toBeTruthy();
  expect(xlsx.headers()["content-type"]).toContain("spreadsheetml");
});
