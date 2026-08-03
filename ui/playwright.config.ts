import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "../tests/ui",
  testMatch: "parity.spec.ts",
  outputDir: "../tests/ui/test-results",
  fullyParallel: false,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:7070",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
