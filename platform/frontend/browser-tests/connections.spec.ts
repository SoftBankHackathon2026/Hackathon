import { test, expect, type Page } from "@playwright/test";
// Preview tests must never reach a live platform API.
test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({
      status: 501,
      json: { detail: "No fixture for preview API" },
    }),
  );
  await page.route("**/api/v1/access-pin", (route) =>
    route.fulfill({ json: { required: false, unlocked: true } }),
  );
});
async function setup(page: Page) {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        write: async (items: ClipboardItem[]) => {
          const blob = await items[0].getType("text/plain");
          (window as any).__copiedText = await blob.text();
          if ((window as any).__denyCopy) throw new Error("NotAllowedError");
        },
      },
    });
  });
  await page.goto("/?connection-preview=1#prepare");
}
async function scenario(page: Page, name: string) {
  await page.getByText("다른 상태 살펴보기", { exact: true }).click();
  await page.getByLabel("상태 예시").selectOption(name);
  await page.getByRole("button", { name: "선택한 상태 불러오기" }).click();
}
test("only two stages; collection precedes environment metadata and registration", async ({
  page,
}) => {
  await setup(page);
  const progress = page.getByRole("list", {
    name: "환경 연결 진행",
    exact: true,
  });
  await expect(progress.getByRole("listitem")).toHaveCount(2);
  await expect(progress).toContainText("환경정보 수집");
  await expect(progress).toContainText("환경등록");
  await expect(page.getByLabel("환경 이름", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("프로젝트", { exact: true })).toHaveCount(0);
  await page
    .getByLabel("이 환경에 접근할 수 있는 지원 단말을 준비했어요.")
    .check();
  await page.getByRole("button", { name: "미리보기 명령 복사" }).click();
  await page.getByRole("button", { name: "단말 실행 재현" }).click();
  await expect(progress.locator('[aria-current="step"]')).toContainText(
    "환경정보 수집",
  );
  await page.getByLabel("터미널 확인 코드", { exact: true }).fill("ZZZZ7777");
  await page
    .getByLabel("제가 실행한 단말이며, 환경정보 수집을 승인해요.")
    .check();
  await page
    .getByRole("button", { name: "단말 연결 승인", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("4회 남았어요");
  await page.getByLabel("터미널 확인 코드", { exact: true }).fill("ABCD 2345");
  await page
    .getByRole("button", { name: "단말 연결 승인", exact: true })
    .click();
  await page.getByRole("button", { name: "터널 확인 재현" }).click();
  await expect(
    page.getByRole("button", { name: "선택한 환경정보 수집" }),
  ).toBeDisabled();
  await page.getByRole("radio", { name: /busan-production/ }).check();
  await page.getByRole("button", { name: "선택한 환경정보 수집" }).click();
  await expect(progress.locator('[aria-current="step"]')).toContainText(
    "환경등록",
  );
  await expect(page.getByLabel("프로젝트", { exact: true })).toBeVisible();
  await expect(page.getByLabel("환경 이름", { exact: true })).toHaveValue(
    "busan-production",
  );
  await page.getByLabel("프로젝트", { exact: true }).fill("주문 서비스");
  await page.getByLabel("환경 이름", { exact: true }).fill("부산 운영 환경");
  await page.getByRole("button", { name: "환경 등록", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "환경을 등록했어요" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "다음: 앱 선택" }).click();
  await expect(
    page.getByRole("heading", { name: "옮길 앱 고르기" }),
  ).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: "sample-app 선택" }),
  ).toBeChecked();
  await page.getByRole("button", { name: "이전: 환경 연결" }).click();
  await expect(
    page.getByRole("heading", { name: "환경을 등록했어요" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "이 계획 승인" })).toHaveCount(
    0,
  );
  await expect(page.getByText("요청할 접근 범위", { exact: true })).toHaveCount(
    0,
  );
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "환경을 등록했어요" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "부산 운영 환경", exact: true }),
  ).toBeVisible();
});
test("clipboard rejection cancels collection request without reporting success", async ({
  page,
}) => {
  await setup(page);
  await page.evaluate(() => {
    (window as any).__denyCopy = true;
  });
  await page
    .getByLabel("이 환경에 접근할 수 있는 지원 단말을 준비했어요.")
    .check();
  await page.getByRole("button", { name: "미리보기 명령 복사" }).click();
  await expect(page.getByRole("alert")).toContainText("미사용 요청은 취소");
  await expect(page.getByLabel("환경 이름", { exact: true })).toHaveCount(0);
});
for (const [source, provider] of [
  ["aws-seoul", "AWS"],
  ["gcp-seoul", "Google Cloud"],
] as const)
  test(
    "registration uses collected " + provider + " identity",
    async ({ page }) => {
      await setup(page);
      await scenario(page, "connecting");
      await page.getByRole("button", { name: "터널 확인 재현" }).click();
      await page.getByRole("radio", { name: new RegExp(source) }).check();
      await page.getByRole("button", { name: "선택한 환경정보 수집" }).click();
      await expect(page.getByText(provider, { exact: true })).toBeVisible();
      await expect(page.getByLabel("환경 이름", { exact: true })).toHaveValue(
        source,
      );
      await expect(page.getByRole("radio")).toHaveCount(0);
      await page.getByLabel("프로젝트", { exact: true }).fill("서비스 이관");
      await page
        .getByRole("button", { name: "환경 등록", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "환경을 등록했어요" }),
      ).toBeVisible();
    },
  );
test("revocation retains separate cleanup statuses", async ({ page }) => {
  await setup(page);
  await scenario(page, "registered");
  await page.getByRole("button", { name: "연결 해제", exact: true }).click();
  await page.getByRole("button", { name: "연결 해제 확인" }).click();
  await expect(
    page.getByRole("heading", { name: "연결 해제를 확인하고 있어요" }),
  ).toBeVisible();
  await expect(page.getByText("미확인", { exact: true })).toHaveCount(2);
});
for (const width of [390, 768, 1024, 1600])
  test(
    "two-stage layout fits " + width,
    async ({ page, browserName }, info) => {
      await page.setViewportSize({ width, height: 1000 });
      await setup(page);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        ),
      ).toBe(false);
      if (browserName === "chromium" && width === 1600)
        await page.screenshot({
          path: info.outputPath("collection.png"),
          fullPage: true,
        });
      await scenario(page, "collected");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        ),
      ).toBe(false);
      await expect(
        page.getByRole("heading", { name: "수집한 환경을 등록하세요" }),
      ).toBeVisible();
      if (browserName === "chromium" && (width === 390 || width === 1600))
        await page.screenshot({
          path: info.outputPath("registration.png"),
          fullPage: true,
        });
    },
  );
test("production entry blocks collection and never exposes registration inputs early", async ({
  page,
}) => {
  const writes: string[] = [];
  await page.route("**/api/v1/access", (route) =>
    route.fulfill({
      json: { auth_mode: "demo", execution_configured: false, user: null },
    }),
  );
  page.on("request", (r) => {
    if (r.method() === "POST") writes.push(r.url());
  });
  await page.goto("/#prepare");
  await page
    .getByLabel("이 환경에 접근할 수 있는 지원 단말을 준비했어요.")
    .check();
  await expect(
    page.getByRole("button", { name: "명령 복사", exact: true }),
  ).toBeDisabled();
  await expect(page.getByLabel("환경 이름")).toHaveCount(0);
  expect(writes).toEqual([]);
});
test("real Chromium clipboard works from the initial collection screen", async ({
  page,
  browserName,
  context,
}) => {
  test.skip(
    browserName !== "chromium",
    "실제 클립보드는 Chromium에서 별도 확인합니다.",
  );
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/?connection-preview=1#prepare");
  await page
    .getByLabel("이 환경에 접근할 수 있는 지원 단말을 준비했어요.")
    .check();
  await page.getByRole("button", { name: "미리보기 명령 복사" }).click();
  await expect(
    page.getByRole("button", { name: "단말 실행 재현" }),
  ).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toMatch(
    /^# Oneport/,
  );
});
test("expired request stays within collection", async ({ page }) => {
  await setup(page);
  await scenario(page, "expired");
  await expect(
    page.getByRole("heading", { name: "명령 기한이 끝났어요" }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("list", { name: "환경 연결 진행", exact: true })
      .locator('[aria-current="step"]'),
  ).toContainText("환경정보 수집");
});
