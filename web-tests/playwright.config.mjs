// Browser tests for a built Vantage story (default: the fictional demo in dist/demo-lakeside).
//
//   npx playwright test -c web-tests/playwright.config.mjs --project=iphone-chromium --project=desktop-chromium
//
// WebKit projects need `npx playwright install webkit` (CI does this); locally only Chromium may exist.
// VANTAGE_STORY=<slug> and VANTAGE_DIST=<dist root> point the suite at another built story;
// VANTAGE_URL=<https://…/> tests an already-hosted copy instead of starting `vantage preview`.
import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const slug = process.env.VANTAGE_STORY || "demo-lakeside";
const port = Number(process.env.VANTAGE_PORT || 4173);
const baseURL = process.env.VANTAGE_URL || `http://127.0.0.1:${port}/`;
const iphone = devices["iPhone 15 Pro"];

export default defineConfig({
  testDir: here,
  testMatch: "*.spec.mjs",
  testIgnore: "dev/**",
  outputDir: path.join(here, ".results", "test-results"),
  timeout: 90_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: [
    ["list"],
    ["html", { outputFolder: path.join(here, ".results", "report"), open: "never" }],
  ],
  use: {
    baseURL,
    actionTimeout: 15_000, // a missing control fails in seconds, not at the test timeout
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "iphone-webkit", use: { ...iphone } },
    { name: "iphone-se-webkit", use: { ...devices["iPhone SE"] } },
    { name: "desktop-chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    // iPhone 15 Pro metrics on Chromium: the local stand-in where only Chromium is installed.
    { name: "iphone-chromium", use: { ...iphone, browserName: "chromium" } },
  ],
  webServer: process.env.VANTAGE_URL
    ? undefined
    : {
        command: `uv run vantage preview ${slug} --port ${port}`,
        cwd: path.resolve(here, ".."),
        url: baseURL,
        reuseExistingServer: true,
        timeout: 60_000,
        stdout: "ignore",
        stderr: "pipe",
      },
});
