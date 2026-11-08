import { test, expect, type Page } from "@playwright/test";
import type { ConnectionSnapshot } from "../src/features/prepare/connectionTypes";
import { appWorkloads } from "../tests/fixtures/appWorkloads";

// Mocked API contract regression: route.fulfill supplies API responses and the
// clipboard is replaced with a test double. This is not evidence of a real
// backend, terminal, WireGuard tunnel, or Kubernetes end-to-end connection.
for (const provider of ["kubernetes", "aws"] as const) {
  test(`mocked ${provider} contract copies a command, confirms, collects, registers and restores`, ({
    page,
  }) => exerciseConnection(page, provider));
}

async function exerciseConnection(page: Page, provider: "kubernetes" | "aws") {
  test.setTimeout(60000);
  const cloud = provider === "aws";
  const id = "a".repeat(22);
  const oldId = "b".repeat(22);
  const source = {
    id: "cluster-one",
    kind: provider,
    name: "시험 클러스터",
    identity: "cluster-uid-one",
    region: cloud ? "ap-northeast-2" : "",
    context: "test-context",
  };
  let state: ConnectionSnapshot | undefined;
  let collectionReadyAt = 0;
  let retries = 0;
  const mutations: { action: string; body: any }[] = [];
  let jobRequests = 0;
  let showRegisteredEnvironment = false;
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        write: async (items: ClipboardItem[]) => {
          (window as any).__command = await (
            await items[0].getType("text/plain")
          ).text();
        },
      },
    });
  });
  // Catch every API request so this contract test can never contact a real backend.
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({
      status: 501,
      json: { detail: "No fixture for this API contract" },
    }),
  );
  await page.route("**/api/v1/access-pin", (route) =>
    route.fulfill({ json: { required: false, unlocked: true } }),
  );
  await page.route("**/api/v1/security/posture", (route) =>
    route.fulfill({
      json: {
        source: "live",
        evaluated_at: new Date().toISOString(),
        standards: [],
        environments: [],
        controls: [],
        parities: [],
        parity: {
          source_env: "",
          target_env: "",
          regressions: [],
          improvements: [],
        },
      },
    }),
  );
  await page.route("**/api/v1/access", (route) =>
    route.fulfill({
      json: {
        auth_mode: "login",
        execution_configured: false,
        user: { email: "test@example.com", team: "test-team", role: "owner" },
      },
    }),
  );
  await page.route("**/api/v1/fleet", (route) =>
    route.fulfill({
      json: {
        source: "live",
        generated_at: new Date().toISOString(),
        environments: [
          {
            id: "destination",
            name: "시험 도착 후보",
            provider: "gcp",
            apps: [],
            data_services: [],
            components: [],
            issues: [],
          },
          ...(showRegisteredEnvironment
            ? [
                {
                  id: `connected-${id}`,
                  name: source.name,
                  provider: cloud ? "aws" : "onprem",
                  kind: cloud ? "eks" : "kubernetes",
                  registered: true,
                  project: "시험 프로젝트",
                  apps: [],
                  data_services: [],
                  components: [],
                  issues: [],
                },
              ]
            : []),
        ],
        migrations: [],
      },
    }),
  );
  await page.route("**/api/v1/migrations", (route) => {
    jobRequests++;
    return route.fulfill({
      status: 201,
      json: { job_id: "test-job", label: "Test" },
    });
  });
  await page.route("**/api/v1/connections**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    if (path.endsWith("capabilities"))
      return route.fulfill({
        json: { available: true, providers: ["kubernetes", "aws"] },
      });
    if (request.method() === "GET") {
      if (state?.collectionPending && Date.now() >= collectionReadyAt) {
        state = {
          ...state,
          revision: state.revision + 1,
          collection: "COLLECTED",
          collectionPending: false,
          collected: [{ ...source, facts: [{ label: "노드", value: "1" }] }],
          inventoryDigest: "sha384:test",
          collectedAt: Date.now(),
        };
      }
      return route.fulfill({
        json: state
          ? [
              { ...state, serverNow: Date.now(), observedAt: Date.now() },
              ...(state.device === "PENDING_APPROVAL"
                ? [
                    {
                      ...state,
                      id: oldId,
                      device: "APPROVED",
                      tunnel: "READY",
                      candidate: {
                        ...state.candidate,
                        hostname: "이전 단말",
                      },
                    },
                  ]
                : []),
            ]
          : [],
      });
    }
    const body = request.postDataJSON();
    const action = path.split("/").at(-1)!;
    mutations.push({ action, body });
    if (action === "connections") {
      const issued: ConnectionSnapshot = {
        id,
        revision: 1,
        organization: "test-team",
        target: null,
        provider,
        serverNow: Date.now(),
        observedAt: Date.now(),
        registrationExpiresAt: Date.now() + 60_000,
        registration: "ISSUED",
        device: "NONE",
        tunnel: "NONE",
        collection: "WAITING",
        discovered: [],
        collected: [],
        permissions: {
          approveDevice: true,
          registerEnvironment: true,
          revoke: true,
        },
        attemptsLeft: 5,
        tunnelChecks: 0,
        epoch: 0,
        events: [],
        deployment: {
          bootstrapUrl:
            "https://connect.example.com/releases/live-e2e-1/bootstrap.sh",
          releaseVersion: "test-v1",
        },
      };
      state = {
        ...issued,
        revision: 2,
        registration: "CONSUMED",
        device: "PENDING_APPROVAL",
        approvalExpiresAt: Date.now() + 300_000,
        candidate: {
          id: "candidate",
          digest: "digest",
          fingerprint: "fingerprint",
          hostname: "test",
          os: "Ubuntu",
          arch: "arm64",
          supportsReplacement: true,
        },
      };
      return route.fulfill({ json: issued });
    }
    if (!state)
      return route.fulfill({ status: 404, json: { detail: "missing" } });
    state.revision++;
    if (action === "approve") {
      expect(body.replaceId).toBe(oldId);
      state.device = "APPROVED";
      state.tunnel = "READY";
      state.epoch = 1;
      state.tunnelChecks = 4;
      state.discovered = [source];
      if (cloud) {
        state.discoveryComplete = true;
        state.discoveryWarnings = [
          { code: "PERMISSION_DENIED", scope: "ap-northeast-1" },
        ];
      }
    } else if (action === "rediscover") {
      retries++;
      state.discovered = retries === 1 ? [] : [source];
    } else if (action === "collect") {
      state.collectionPending = true;
      collectionReadyAt = Date.now() + 2000;
    } else if (action === "register") {
      state.target = body.target;
      state.environmentId = "registered-environment";
      state.selectedEnvironmentId = source.id;
      state.registeredAt = Date.now();
    }
    return route.fulfill({ json: state });
  });
  await page.goto("/#prepare");
  await expect(
    page.getByRole("heading", { name: "마이그레이션 작업 등록" }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("list", { name: "작업 등록 단계" })
      .locator("[aria-current]"),
  ).toContainText("출발·도착 환경 선택");
  await page
    .getByRole("button", { name: "환경 연결 추가", exact: true })
    .click();
  if (cloud)
    await page.getByRole("radio", { name: "AWS", exact: true }).check();
  await page
    .getByLabel("이 환경에 접근할 수 있는 지원 단말을 준비했어요.")
    .check();
  await page.getByRole("button", { name: "명령 복사", exact: true }).click();
  await expect
    .poll(() => page.evaluate(() => (window as any).__command))
    .toBeTruthy();
  const command = await page.evaluate(
    () => (window as any).__command as string,
  );
  expect(command).toMatch(
    /MIGRATION_ENROLL='[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}'/,
  );
  const secret = command.match(/\.([A-Za-z0-9_-]{43})'/)![1];
  expect(JSON.stringify(mutations)).not.toContain(secret);
  expect(await page.locator("body").innerText()).not.toContain(secret);
  await page.getByLabel("터미널 확인 코드", { exact: true }).fill("abcd-2345");
  await page
    .getByLabel("제가 실행한 단말이며, 환경정보 수집을 승인해요.")
    .check();
  await page
    .getByRole("combobox", { name: "교체할 연결", exact: true })
    .selectOption(oldId);
  await expect(
    page.getByText(/선택한 연결과 진행 중인 환경정보 수집이 종료/),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "기존 연결을 종료하고 교체", exact: true })
    .click();
  if (cloud) {
    await expect(page.getByRole("alert")).toContainText(
      "클러스터를 조회할 권한이 없어요.",
    );
    await expect(
      page.getByRole("radio", { name: /시험 클러스터/ }),
    ).toBeEnabled();
    await page.getByRole("button", { name: "다시 확인" }).click();
    await expect(page.getByText("선택할 수 있는 대상이 없어요.")).toBeVisible();
    await page.getByRole("button", { name: "이 환경 건너뛰기" }).click();
    await expect(
      page.getByRole("heading", { name: "먼저 환경정보를 수집할게요" }),
    ).toBeVisible();
    await page.getByRole("button", { name: /^환경 연결 요청 AWS / }).click();
    await page.getByRole("button", { name: "다시 확인" }).click();
  }
  await page.getByRole("radio", { name: /시험 클러스터/ }).check();
  await page.getByRole("button", { name: "선택한 환경정보 수집" }).click();
  await expect(
    page.getByRole("button", { name: "환경정보 수집 중…" }),
  ).toBeDisabled();
  await expect(
    page.getByText("환경정보를 수집했어요.", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByLabel("환경 역할", { exact: true })).toHaveCount(0);
  await page.getByLabel("프로젝트", { exact: true }).fill("시험 프로젝트");
  await page.getByRole("button", { name: "환경 등록", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "환경을 등록했어요" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "작업 등록으로 돌아가기", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "출발 환경" })
    .selectOption("registered-environment");
  const destination = page.getByRole("combobox", { name: "도착 환경" });
  await expect(
    destination.locator("option[value='registered-environment']"),
  ).toHaveCount(0);
  await destination.selectOption("destination");
  await page.getByRole("button", { name: "다음: 앱 선택" }).click();
  await expect(
    page.getByRole("heading", { name: "옮길 앱 고르기" }),
  ).toBeVisible();
  await expect(
    page.getByText(/이전 수집 결과에는 앱 목록이 없어요/),
  ).toBeVisible();
  await expect(page.getByText("sample-app", { exact: true })).toHaveCount(0);
  state!.workloads = {
    coverage: "complete",
    items: appWorkloads,
  };
  state!.inventoryDigest = "updated-inventory";
  const picker = page.getByRole("region", { name: "옮길 앱 고르기" });
  const apps = picker.getByRole("button", { name: "앱 6개", exact: true });
  const data = picker.getByRole("button", { name: /^데이터·미들웨어 / });
  const migration = picker.getByRole("button", { name: /^이전 도구 / });
  const infrastructure = picker.getByRole("button", {
    name: /^인프라·시스템 /,
  });
  const choices = picker.getByRole("checkbox", { name: / 선택$/ });
  const search = picker.getByRole("searchbox", { name: "앱 검색" });

  await expect(apps).toBeVisible({ timeout: 10000 });
  await expect(apps).toHaveAttribute("aria-expanded", "true");
  await expect(data).toHaveAttribute("aria-expanded", "false");
  await expect(migration).toHaveAttribute("aria-expanded", "false");
  await expect(infrastructure).toHaveAttribute("aria-expanded", "false");
  await expect(choices).toHaveCount(6);
  await expect(choices.last()).toHaveAccessibleName(
    "hackathon/Deployment/ai-agent 선택",
  );
  await expect(picker.getByText("정지됨", { exact: true })).toBeVisible();

  const review = picker.getByRole("checkbox", {
    name: "hackathon/Deployment/review-api 선택",
  });
  await review.check();
  await search.fill("APACHE/KAFKA");
  await expect(
    picker.getByText("접힌 그룹에 2개 있어요.", { exact: false }),
  ).toBeVisible();
  await expect(
    picker.getByText("선택한 앱 1개", { exact: true }),
  ).toBeVisible();
  await data.click();
  await expect(choices).toHaveCount(2);
  await search.fill("");
  await expect(review).toBeChecked();

  const namespace = picker.getByRole("combobox", { name: "네임스페이스" });
  await namespace.selectOption("harbor");
  await expect(
    picker.getByText("접힌 그룹에 7개 있어요.", { exact: false }),
  ).toBeVisible();
  await picker.getByRole("checkbox", { name: "인프라도 보기" }).check();
  await expect(choices).toHaveCount(7);
  await search.fill("없는-워크로드");
  await expect(
    picker.getByText("검색·필터에 맞는 항목이 없어요.", { exact: true }),
  ).toBeVisible();
  await search.fill("");
  await namespace.selectOption("");

  await migration.click();
  await expect(choices).toHaveCount(51);
  const tool = picker.getByRole("checkbox", {
    name: "hackathon/Deployment/db-insert-worker 선택",
  });
  await tool.check();
  await expect(tool).toBeChecked();
  await expect(
    picker.getByText("이 항목은 옮기는 대상이 아닐 수 있어요.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    picker.getByText("선택한 앱 2개", { exact: true }),
  ).toBeVisible();
  await search.fill("db-insert");
  state!.inventoryDigest = "recollected-inventory";
  state!.revision++;
  await expect(picker.getByText("선택한 앱 0개", { exact: true })).toBeVisible({
    timeout: 10000,
  });
  await expect(search).toHaveValue("db-insert");
  await expect(migration).toHaveAttribute("aria-expanded", "true");
  await expect(tool).not.toBeChecked();
  await tool.check();
  await page
    .getByRole("button", { name: "환경 연결 추가", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "새 환경 연결" }),
  ).toBeVisible();
  await page.goBack();
  await expect(
    picker.getByText("선택한 앱 1개", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "이전: 환경 선택" }).click();
  const departure = page.getByRole("combobox", { name: "출발 환경" });
  await departure.selectOption("destination");
  await expect(destination).toHaveValue("");
  await destination.selectOption("registered-environment");
  await page.getByRole("button", { name: "다음: 앱 선택" }).click();
  await expect(
    page.getByText("이 출발 환경에서 확인한 앱이 없어요."),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "다음: 작업 등록" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "이전: 환경 선택" }).click();
  await departure.selectOption("registered-environment");
  await destination.selectOption("destination");
  await page.getByRole("button", { name: "다음: 앱 선택" }).click();
  await expect(
    picker.getByText("선택한 앱 0개", { exact: true }),
  ).toBeVisible();
  await picker.getByRole("button", { name: /^이전 도구 / }).click();
  await tool.check();
  await page.getByRole("button", { name: "다음: 작업 등록" }).click();
  await expect(
    page.getByRole("heading", { name: "작업 등록", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "작업 만들기 (앱 1개)" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "이전: 앱 선택" }).click();
  await expect(
    picker.getByText("선택한 앱 1개", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "다음: 작업 등록" }).click();
  const created = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/migrations",
  );
  await page
    .getByRole("button", { name: "작업 만들기 (앱 1개)" })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
      button.click();
    });
  expect((await created).postDataJSON()).toEqual({
    source_env: "registered-environment",
    source_provider: cloud ? "aws" : "onprem",
    target_env: "destination",
    target_provider: "gcp",
    apps: ["hackathon/Deployment/db-insert-worker"],
  });
  await expect(page).toHaveURL(/#migrations\/test-job$/);
  expect(jobRequests).toBe(1);
  await page.goto(`/#prepare/${id}`);
  expect(mutations.find((item) => item.action === "approve")?.body.code).toBe(
    "ABCD2345",
  );
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "환경을 등록했어요" }),
  ).toBeVisible();
  expect(mutations.map((item) => item.action)).toEqual([
    "connections",
    "approve",
    ...(cloud ? ["rediscover", "rediscover"] : []),
    "collect",
    "register",
  ]);
  expect(
    mutations.find((item) => item.action === "connections")?.body.provider,
  ).toBe(cloud ? "aws" : undefined);
  expect(
    mutations.find((item) => item.action === "register")?.body.target,
  ).not.toHaveProperty("role");
  showRegisteredEnvironment = true;
  await page.goto("/#environments");
  await expect(
    page.getByRole("button", { name: "아키텍처", exact: true }),
  ).toBeVisible();
  const filter = page
    .getByRole("group", { name: "필터" })
    .getByRole("combobox");
  await filter.selectOption(`connected-${id}`);
  await expect(
    page
      .getByRole("complementary", { name: "환경 요약" })
      .getByRole("link", { name: "직접 등록, 시험 프로젝트" }),
  ).toHaveAttribute("href", `#prepare/${id}`);
  await expect(
    page.getByRole("region", { name: "직접 등록한 환경" }),
  ).toHaveCount(0);
}
