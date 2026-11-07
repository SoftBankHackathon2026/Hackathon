import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const fixture = JSON.parse(
  readFileSync(new URL("./fixtures/layout.json", import.meta.url), "utf8"),
);

// Fixture: backend SimulatedFleet(DEFAULT_FLEET), FleetService, MigrationService,
// SecurityService; synthetic accounts/connections/costs and 5 series points.
// Never a live cluster. Every API call is intercepted.
// Wide tables and the pannable SVG canvas may overflow their own bounded region;
// body overflow-x:clip must not hide accidental page-level overflow from this test.
const cases = [
  ["environments"],
  ["environments", "이전 경로"],
  ["environments", "보안"],
  ["environments/nodes"],
  ["environments/onprem/cloudflared"],
  ["environments/nodes/onprem"],
  ["migrations"],
  ["migrations/forward_001"],
  ["agent"],
  ["agent/run-layout"],
  ["prepare"],
  ["prepare/" + "a".repeat(22)],
  ["prepare/" + "a".repeat(22), "apps-loading"],
  ["prepare/" + "b".repeat(22), "apps-loaded"],
  ["security"],
  ["security", "발견 사항"],
  ["security", "표준"],
  ["security/NET-01"],
  ["costs"],
  ["costs/migration"],
  ["costs/environments"],
  ["team"],
  ["login"],
  ["login", "signup"],
];

for (const width of [390, 524, 768, 1024, 1440]) {
  for (const [route, state] of cases) {
    test(`${route} ${state ?? ""} fits ${width}px`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 });
      const data: Record<string, any> = structuredClone(fixture);
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      if (route === "login") data.access.user = null;
      for (const connection of data.connections) {
        connection.collectionPending = state === "apps-loading";
        connection.workloads =
          state === "apps-loaded"
            ? {
                coverage: "complete",
                items: [
                  {
                    namespace: "demo",
                    kind: "Deployment",
                    name: "production-inventory-migration-with-a-very-long-workload-name",
                    replicas: 1,
                    images: ["example/demo:v1"],
                    secrets: [],
                    configMaps: [],
                    pvcs: [],
                  },
                ],
              }
            : null;
      }
      await page.route("**/api/v1/**", (request) => {
        const key = new URL(request.request().url()).pathname.slice(8);
        return request.fulfill(
          data[key] !== undefined
            ? { json: data[key] }
            : { status: 404, json: { detail: "No layout fixture: " + key } },
        );
      });
      await page.goto("/#" + (state?.startsWith("apps-") ? "prepare" : route));
      await expect(page.locator("h1").first()).toBeVisible();
      if (state?.startsWith("apps-")) {
        await page
          .getByRole("combobox", { name: "출발 환경", exact: true })
          .selectOption(state === "apps-loading" ? "env-0" : "env-1");
        await page
          .getByRole("combobox", { name: "도착 환경", exact: true })
          .selectOption("aws");
        for (const label of await page
          .locator(".connection-fields label")
          .all()) {
          expect((await label.boundingBox())!.width).toBeGreaterThan(100);
          const caption = await label.evaluate((element) => {
            const range = document.createRange();
            range.selectNodeContents(element.firstChild!);
            return {
              height: range.getBoundingClientRect().height,
              lineHeight: parseFloat(getComputedStyle(element).lineHeight),
            };
          });
          expect(caption.height).toBeLessThanOrEqual(caption.lineHeight * 1.5);
        }
        await page.getByRole("button", { name: "다음: 앱 선택" }).click();
        await expect(
          page.getByRole("heading", { name: "옮길 앱 고르기" }),
        ).toBeVisible();
        if (state === "apps-loading") {
          await expect(
            page.getByText("앱 목록을 수집하고 있어요", { exact: true }),
          ).toBeVisible();
          await expect(
            page.getByRole("button", { name: "다음: 작업 등록" }),
          ).toBeDisabled();
        } else {
          await expect(
            page.getByRole("checkbox", { name: / 선택$/ }),
          ).toHaveCount(1);
          await page.getByRole("checkbox", { name: / 선택$/ }).check();
          await expect(
            page.getByRole("button", { name: "다음: 작업 등록" }),
          ).toBeEnabled();
        }
      } else if (state === "signup") {
        await page
          .getByRole("button", { name: "처음이신가요? 계정 만들기" })
          .click();
      } else if (route === "environments" && state) {
        await page.getByRole("tab", { name: state, exact: true }).click();
        if (state === "보안")
          await page
            .getByRole("button", { name: "심각도", exact: true })
            .click();
      } else if (state) {
        await page.getByRole("button", { name: state, exact: true }).click();
      }
      await page.evaluate(() => document.fonts.ready);
      await page.evaluate(() => window.scrollTo(0, 0));
      const overflow = await page.evaluate(() => {
        const boundedScroll = (element: HTMLElement) => {
          for (
            let parent = element.parentElement;
            parent;
            parent = parent.parentElement
          ) {
            const box = parent.getBoundingClientRect();
            if (
              ["auto", "scroll"].includes(getComputedStyle(parent).overflowX) &&
              box.left >= 0 &&
              box.right <= innerWidth + 1
            )
              return true;
          }
          return false;
        };
        return [...document.querySelectorAll("main *, .page-head *, .login *")]
          .filter(
            (e): e is HTMLElement =>
              e instanceof HTMLElement && e.checkVisibility(),
          )
          .filter((e) => {
            const r = e.getBoundingClientRect();
            return (
              r.width > 0 &&
              (r.left < -1 || r.right > innerWidth + 1) &&
              !boundedScroll(e)
            );
          })
          .map(
            (e) =>
              `${e.tagName}.${e.className}: ${e.textContent?.slice(0, 60)}`,
          );
      });
      expect(overflow).toEqual([]);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width + 1);
      expect(errors).toEqual([]);
      if (route === "environments") {
        const region = page.getByRole("region", { name: "등록 환경 목록" });
        await expect(region).toBeVisible();
        await region.focus();
        if (width < 768) {
          await page.keyboard.press("ArrowRight");
          await expect
            .poll(() => region.evaluate((e) => e.scrollLeft))
            .toBeGreaterThan(0);
        }
      }
      if (state?.startsWith("apps-") || route === "environments") {
        await page.screenshot({ path: testInfo.outputPath("layout.png") });
      }
      if (route === "team") {
        await page.locator(".account-menu summary").click();
        const menu = await page.locator(".account-menu .menu").boundingBox();
        expect(menu!.x).toBeGreaterThanOrEqual(0);
        expect(menu!.x + menu!.width).toBeLessThanOrEqual(width);
      }
    });
  }
}
