import { expect, test } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SAMPLE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../data/synthetic/dev/case_0004.pdf");
const SHOTS = process.env.SCREENSHOT_DIR;

test("dev sign-in is required", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.locator('input[name="user"]').fill("e2e-signin");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("whoami")).toContainText("e2e-signin");
});

test("upload → pipeline → click citation → highlighted source → review", async ({ page, context, baseURL }) => {
  await context.addCookies([{ name: "medchron_dev", value: "e2e-attorney:attorney", url: baseURL! }]);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "New matter" })).toBeVisible();

  // Upload a synthetic production and run the demo engine.
  await page.locator('input[name="file"]').setInputFiles(SAMPLE);
  await page.locator('input[name="matter_id"]').fill("e2e-case-0004");
  await page.locator('input[name="title"]').fill("Synthetic case 4 (bile leak after cholecystectomy)");
  await page.locator('select[name="provider"]').selectOption("oracle");
  await page.getByRole("button", { name: "Upload and run" }).click();

  // The worker processes the job, and the chronology appears once it finishes.
  const rows = page.getByTestId("event-row");
  await expect(rows.first()).toBeVisible({ timeout: 60_000 });
  expect(await rows.count()).toBeGreaterThan(10);
  await expect(page.getByTestId("for-review")).toContainText("possible missing records");
  await expect(page.getByTestId("for-review")).toContainText("prompt injection");
  await expect(page.getByTestId("for-review")).toContainText("possible misfiled page");

  // Clicking a citation opens the source page and highlights the quoted text.
  const ctRow = rows.filter({ hasText: "Hepatobiliary scan: bile leak" }).first();
  await ctRow.getByTestId("cite").first().click();
  await expect(page.getByTestId("highlight").first()).toBeVisible();
  await expect(page.getByTestId("cited-quote")).toContainText("Bile leak");
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

  // Checkpoint 3: triage every issue (dismissals need a reason, entered in a dialog).
  page.on("dialog", (d) => d.accept("Injection text, noted for the file."));
  const issues = page.getByTestId("issue");
  await expect(issues.first()).toBeVisible();
  const n = await issues.count();
  expect(n).toBeGreaterThan(2);
  await expect(page.getByTestId("draft-memo")).toBeDisabled();
  for (let k = 0; k < n; k++) {
    const issue = issues.nth(k);
    const isInjection = (await issue.textContent())?.includes("prompt injection");
    await issue.getByRole("button", { name: isInjection ? "Dismiss" : "Ask expert" }).click();
    await expect(issue).toContainText("triage:");
  }

  // Checkpoint 4: draft the cited memo, check a citation, approve it.
  await page.getByTestId("draft-memo").click();
  const memoBody = page.getByTestId("memo-body");
  await expect(memoBody).toContainText("DRAFT", { timeout: 60_000 });
  await memoBody.locator(".cite").first().click();
  await expect(page.getByTestId("highlight").first()).toBeVisible();
  if (SHOTS) {
    await page.getByTestId("issues").scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${SHOTS}/issues-memo.png` });
  }
  await page.getByTestId("approve-memo").click();
  await expect(page.getByTestId("memo")).toContainText("Approved by");
  await expect(page.getByTestId("memo").getByRole("link", { name: "Export (.md)" })).toBeVisible();

  // The review state survives a reload.
  await page.reload();
  await expect(page.getByTestId("event-row").filter({ hasText: "Hepatobiliary scan: bile leak" }).first()).toContainText(
    "accepted",
  );

  // The export reflects reviewer decisions.
  const exported = await page.request.get("/api/matters/e2e-case-0004/export.md");
  expect(exported.ok()).toBeTruthy();
  expect(await exported.text()).toContain("review:accepted");

  if (SHOTS) {
    await page.goto("/");
    await page.screenshot({ path: `${SHOTS}/matters.png` });
  }
});
