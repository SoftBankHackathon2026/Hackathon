import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

const fixture = JSON.parse(
  readFileSync(new URL("./fixtures/layout.json", import.meta.url), "utf8"),
);

for (const width of [390, 768, 1440]) {
  test(`fleet app selection aligns rows and preserves choices at ${width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const data = structuredClone(fixture);
    data.connections = [];
    const longName = "production".repeat(16);
    data.fleet.environments
      .find((env: { id: string }) => env.id === "onprem")
      .apps.push({ name: longName });
    // No real backend, connection, or migration is used in this layout regression.
    await page.route("**/api/v1/**", (route) => {
      const key = new URL(route.request().url()).pathname.slice(8);
      return route.fulfill(
        data[key] !== undefined
          ? { json: data[key] }
          : { status: 404, json: { detail: "No fixture: " + key } },
      );
    });
    await page.goto("/#prepare");
    await page
      .getByRole("combobox", { name: "출발 환경", exact: true })
      .selectOption("onprem");
    await page
      .getByRole("combobox", { name: "도착 환경", exact: true })
      .selectOption("aws");
    await page.getByRole("button", { name: "다음: 앱 선택" }).click();
    const apps = page.getByRole("group", { name: "출발 환경에서 옮길 앱" });
    await expect(apps).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const row = apps.locator("label").filter({ hasText: "sample-app" });
    const geometry = await row.evaluate((label) => {
      const input = label.querySelector("input")!.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(label.lastChild!);
      const name = range.getBoundingClientRect();
      return {
        inputRight: input.right,
        nameLeft: name.left,
        overlap:
          Math.min(input.bottom, name.bottom) - Math.max(input.top, name.top),
      };
    });
    expect(geometry.nameLeft).toBeGreaterThan(geometry.inputRight);
    expect(geometry.overlap).toBeGreaterThan(0);
    await expect(
      page.getByRole("button", { name: "다음: 작업 등록" }),
    ).toBeDisabled();
    // Clicking the name and keyboard Space both toggle the native checkbox.
    await row.click();
    const checkbox = apps.getByRole("checkbox", {
      name: "sample-app",
      exact: true,
    });
    await expect(checkbox).toBeChecked();
    await checkbox.focus();
    await page.keyboard.press("Space");
    await expect(checkbox).not.toBeChecked();
    await page.keyboard.press("Space");
    await apps.getByRole("checkbox", { name: longName, exact: true }).check();
    const bounds = await apps.locator("label").evaluateAll((rows) =>
      rows.map((row) => ({
        width: row.clientWidth,
        contentWidth: row.scrollWidth,
        right: row.getBoundingClientRect().right,
      })),
    );
    for (const bound of bounds) {
      expect(bound.contentWidth).toBeLessThanOrEqual(bound.width + 1);
      expect(bound.right).toBeLessThanOrEqual(width);
    }
    await page.screenshot({
      path: testInfo.outputPath("app-selection.png"),
      fullPage: true,
    });
    await page.getByRole("button", { name: "다음: 작업 등록" }).click();
    await expect(page.locator(".connection-task-review li")).toHaveText([
      "sample-app",
      longName,
    ]);
    await page.getByRole("button", { name: "이전: 앱 선택" }).click();
    await expect(checkbox).toBeChecked();
  });
}
