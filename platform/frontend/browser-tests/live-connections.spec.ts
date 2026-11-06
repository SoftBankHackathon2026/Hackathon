import { test, expect } from "@playwright/test";
import type { ConnectionSnapshot } from "../src/features/prepare/connectionTypes";

// This verifies the live HTTP/UI boundary with a controlled server substitute;
// it does not claim a terminal or WireGuard end-to-end test.
test("live HTTP flow copies a command, confirms, collects, registers and restores", async ({
  page,
}) => {
  const id = "a".repeat(22);
  const source = {
    id: "cluster-one",
    kind: "kubernetes" as const,
    name: "시험 클러스터",
    identity: "cluster-uid-one",
    region: "",
    context: "test-context",
  };
  let state: ConnectionSnapshot | undefined;
  const mutations: { action: string; body: any }[] = [];
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
        environments: [],
        migrations: [],
      },
    }),
  );
  await page.route("**/api/v1/connections**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    if (path.endsWith("capabilities"))
      return route.fulfill({ json: { available: true } });
    if (request.method() === "GET")
      return route.fulfill({
        json: state
          ? [{ ...state, serverNow: Date.now(), observedAt: Date.now() }]
          : [],
      });
    const body = request.postDataJSON();
    const action = path.split("/").at(-1)!;
    mutations.push({ action, body });
    if (action === "connections") {
      const issued: ConnectionSnapshot = {
        id,
        revision: 1,
        organization: "test-team",
        target: null,
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
          bootstrapUrl: "https://connect.example.com/i",
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
        },
      };
      return route.fulfill({ json: issued });
    }
    if (!state)
      return route.fulfill({ status: 404, json: { detail: "missing" } });
    state.revision++;
    if (action === "approve") {
      state.device = "APPROVED";
      state.tunnel = "READY";
      state.epoch = 1;
      state.tunnelChecks = 4;
      state.discovered = [source];
    } else if (action === "collect") {
      state.collection = "COLLECTED";
      state.collected = [{ ...source, facts: [{ label: "노드", value: "1" }] }];
      state.inventoryDigest = "sha384:test";
      state.collectedAt = Date.now();
    } else if (action === "register") {
      state.target = body.target;
      state.environmentId = "registered-environment";
      state.selectedEnvironmentId = source.id;
      state.registeredAt = Date.now();
    }
    return route.fulfill({ json: state });
  });
  await page.goto("/#prepare");
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
  await page.getByLabel("터미널 확인 코드", { exact: true }).fill("ABCD2345");
  await page
    .getByLabel("제가 실행한 단말이며, 환경정보 수집을 승인해요.")
    .check();
  await page
    .getByRole("button", { name: "단말 연결 승인", exact: true })
    .click();
  await page.getByRole("radio", { name: /시험 클러스터/ }).check();
  await page.getByRole("button", { name: "선택한 환경정보 수집" }).click();
  await page.getByLabel("프로젝트", { exact: true }).fill("시험 프로젝트");
  await page.getByRole("button", { name: "환경 등록", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "환경을 등록했어요" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "다음: 앱 선택" }),
  ).toBeDisabled();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "환경을 등록했어요" }),
  ).toBeVisible();
  expect(mutations.map((item) => item.action)).toEqual([
    "connections",
    "approve",
    "collect",
    "register",
  ]);
  await page.goto("/#environments");
  const records = page.getByRole("region", { name: "직접 등록한 환경" });
  await expect(records.getByText("시험 프로젝트")).toBeVisible();
  await expect(records.getByRole("link")).toHaveAttribute(
    "href",
    `#prepare/${id}`,
  );
});
