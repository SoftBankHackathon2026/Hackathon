import { test, expect, type Page } from "@playwright/test";

async function setup(page: Page) {
  await page.addInitScript(() => {
    // Functional tests control permission failures. Real clipboard has a
    // separate browser test and is not conflated with these simulated writes.
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
  await page.getByLabel("프로젝트", { exact: true }).fill("주문 서비스 이관");
  await page.getByLabel("환경 이름", { exact: true }).fill("busan-production");
}
async function scenario(page: Page, name: string) {
  await page.getByText("다른 상태 살펴보기", { exact: true }).click();
  await page.getByLabel("상태 예시").selectOption(name);
  await page.getByRole("button", { name: "선택한 상태 불러오기" }).click();
}
test("common enrollment, code, explicit context, immutable plan and result", async ({
  page,
}) => {
  await setup(page);
  await page.getByRole("button", { name: "실행 준비 확인" }).click();
  await expect(
    page.getByRole("button", { name: "미리보기 명령 복사" }),
  ).toBeDisabled();
  await page
    .getByLabel("이 환경에 접근할 수 있는 지원 단말을 준비했어요.")
    .check();
  await page.getByRole("button", { name: "미리보기 명령 복사" }).click();
  await expect(
    page.getByRole("button", { name: "단말 실행 재현" }),
  ).toBeVisible();
  expect(await page.evaluate(() => (window as any).__copiedText)).toMatch(
    /^# Oneport/,
  );
  await page.getByRole("button", { name: "단말 실행 재현" }).click();
  await expect(
    page.getByRole("heading", { name: "실행한 단말을 확인하세요" }),
  ).toBeVisible();
  await page.getByLabel("터미널 확인 코드", { exact: true }).fill("ZZZZ9999");
  await page
    .getByLabel("제가 실행한 단말이며, 연결 대상과 범위를 확인했어요.")
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
    page.getByRole("button", { name: "대상 확인 후 계획 받기" }),
  ).toBeDisabled();
  await page.getByLabel("Kubernetes 문맥").selectOption("busan-production");
  await page.getByRole("button", { name: "대상 확인 후 계획 받기" }).click();
  await expect(
    page.getByRole("button", { name: "이 계획 승인", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel(
      "대상·작업·권한·실패 시 처리를 확인했고, 이 계획의 실행을 승인해요.",
    )
    .check();
  await page.getByRole("button", { name: "이 계획 승인", exact: true }).click();
  await page.getByRole("button", { name: "작업 검증 재현" }).click();
  await expect(
    page.getByRole("heading", { name: "환경 준비를 마쳤어요" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "다음: 앱 선택" }),
  ).toBeDisabled();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "환경 준비를 마쳤어요" }),
  ).toBeVisible();
  expect(await page.locator("body").innerText()).not.toContain(
    "MIGRATION_ENROLL='",
  );
});

test("clipboard rejection cancels unused request and never reports success", async ({
  page,
}) => {
  await setup(page);
  await page.evaluate(() => {
    (window as any).__denyCopy = true;
  });
  await page.getByRole("button", { name: "실행 준비 확인" }).click();
  await page
    .getByLabel("이 환경에 접근할 수 있는 지원 단말을 준비했어요.")
    .check();
  await page.getByRole("button", { name: "미리보기 명령 복사" }).click();
  await expect(page.getByRole("alert")).toContainText("미사용 요청은 취소");
  await expect(
    page.getByText("연결한 환경이 없어요.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("미리보기 안내를 복사했어요.", { exact: false }),
  ).not.toBeVisible();
});

test("revocation keeps unconfirmed cleanup visible", async ({ page }) => {
  await setup(page);
  await scenario(page, "active");
  await page.getByRole("button", { name: "연결 해제", exact: true }).click();
  await page.getByRole("button", { name: "연결 해제 확인" }).click();
  await expect(
    page.getByRole("heading", { name: "연결을 해제했어요" }),
  ).toBeVisible();
  await expect(page.getByText("미확인", { exact: true })).toHaveCount(2);
  await expect(
    page.getByText("접속할 수 없는 단말의 파일", { exact: false }),
  ).toBeVisible();
});

test("result unknown never offers change retry", async ({ page }) => {
  await setup(page);
  await scenario(page, "unknown");
  await expect(
    page.getByRole("heading", { name: "실제 작업 결과를 확인해야 해요" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /재실행|재시도/ })).toHaveCount(
    0,
  );
});

test("three providers support the same reviewed connection flow", async ({
  page,
}) => {
  await setup(page);
  await page.getByRole("radio", { name: /Google Cloud/ }).check();
  await expect(page.getByLabel("리전", { exact: true })).toHaveValue(
    "asia-northeast3",
  );
  await scenario(page, "plan");
  await expect(
    page.getByText("Google Cloud 신원 연합 설정", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("저장 데이터 읽기", { exact: true }),
  ).toBeVisible();
});

for (const width of [390, 768, 1024, 1600])
  test(
    "layout stays within viewport at " + width,
    async ({ page, browserName }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 });
      await setup(page);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      );
      expect(overflow).toBe(false);
      if (browserName === "chromium" && width === 1600)
        await page.screenshot({
          path: testInfo.outputPath("environment-selection.png"),
          fullPage: true,
        });
      await scenario(page, "plan");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        ),
      ).toBe(false);
      if (browserName === "chromium" && (width === 1600 || width === 390))
        await page.screenshot({
          path: testInfo.outputPath("plan-review.png"),
          fullPage: true,
        });
    },
  );

test("production entry never creates enrollment or uses legacy endpoints", async ({
  page,
}) => {
  const writes: string[] = [];
  await page.route("**/api/v1/access", (route) =>
    route.fulfill({
      json: { auth_mode: "demo", execution_configured: false, user: null },
    }),
  );
  page.on("request", (request) => {
    if (request.method() === "POST") writes.push(request.url());
  });
  await page.goto("/#prepare");
  await expect(page.getByRole("status")).toContainText("연결 서비스 준비 중");
  await page.getByLabel("조직", { exact: true }).fill("팀");
  await page.getByLabel("프로젝트", { exact: true }).fill("이관");
  await page.getByLabel("환경 이름", { exact: true }).fill("온프레미스");
  await page.getByRole("button", { name: "실행 준비 확인" }).click();
  await page
    .getByLabel("이 환경에 접근할 수 있는 지원 단말을 준비했어요.")
    .check();
  await expect(
    page.getByRole("button", { name: "명령 복사", exact: true }),
  ).toBeDisabled();
  expect(writes).toEqual([]);
});

test("actual clipboard uses one user click without awaiting outside the click", async ({
  page,
  browserName,
  context,
}) => {
  test.skip(
    browserName !== "chromium",
    "실제 클립보드 권한은 Chromium에서 별도로 검증합니다.",
  );
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/?connection-preview=1#prepare");
  await page.getByLabel("프로젝트", { exact: true }).fill("실제 복사 시험");
  await page.getByLabel("환경 이름", { exact: true }).fill("clipboard-test");
  await page.getByRole("button", { name: "실행 준비 확인" }).click();
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

for (const [state, heading] of [
  ["expired", "등록 기한이 끝났어요"],
  ["rekeying", "연결 키를 교체하고 있어요"],
  ["degraded", "연결 복구가 필요해요"],
  ["partial", "일부 작업을 마치지 못했어요"],
] as const)
  test("failure and recovery heading reflects " + state, async ({ page }) => {
    await setup(page);
    await scenario(page, state);
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "다음: 앱 선택" }),
    ).toHaveCount(0);
  });
