import { defineConfig } from "@playwright/test";

const port = Number(process.env.PORT ?? 8765);

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  retries: 0,
  workers: 1, // tests share one API, worker and database
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    viewport: { width: 1440, height: 900 },
    // README screenshots are taken at 2x for sharpness; regular runs use 1x.
    deviceScaleFactor: process.env.SCREENSHOT_DIR ? 2 : 1,
    launchOptions: process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {},
  },
  webServer: {
    command: "../scripts/e2e-server.sh",
    url: `http://127.0.0.1:${port}/healthz`,
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: "pipe",
    env: { PORT: String(port), E2E_DATABASE_URL: process.env.E2E_DATABASE_URL ?? "" },
  },
});
