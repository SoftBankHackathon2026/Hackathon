import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./browser-tests",
  fullyParallel: true,
  workers: 3,
  timeout: 30000,
  use: { baseURL: "http://127.0.0.1:4320", trace: "retain-on-failure" },
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    { name: "firefox", use: { browserName: "firefox" } },
    { name: "webkit", use: { browserName: "webkit" } },
  ],
  webServer: {
    command: "npm run dev -- --strictPort",
    url: "http://127.0.0.1:4320",
    reuseExistingServer: true,
  },
});
