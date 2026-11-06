import type {
  ConnectionGateway,
  ConnectionSnapshot,
  ConnectionPlan,
} from "./connectionTypes.ts";
import {
  canApproveDevice,
  canApprovePlan,
  normalizeCode,
  targetValid,
} from "./model.ts";

export const PREVIEW_CODE = "ABCD2345";
export const PREVIEW_SCENARIOS = [
  ["pending", "단말 승인 대기"],
  ["connecting", "보안 연결 중"],
  ["plan", "계획 검토"],
  ["active", "준비 완료"],
  ["expired", "등록 만료"],
  ["rekeying", "키 교체"],
  ["degraded", "연결 장애"],
  ["partial", "일부 작업 실패"],
  ["unknown", "결과 불명"],
  ["blocked", "인증 제한"],
  ["revoked", "해제·정리 미확인"],
] as const;
export type PreviewScenario = (typeof PREVIEW_SCENARIOS)[number][0];
const STORAGE = "oneport.connection-preview.v1";
const digest = (char: string) => "sha384:" + char.repeat(96);

async function planFor(s: ConnectionSnapshot): Promise<ConnectionPlan> {
  const onprem = s.target.kind === "kubernetes";
  const definition = onprem
    ? `# 화면 검토용 작업 정의 예시입니다. 실제 설치 묶음이 아닙니다.
- name: 승인한 문맥의 환경 준비
  hosts: approved_connector
  tasks:
    - name: 필요한 지원 구성 조사
      oneport.environment_inspect:
        context: "${s.selectedContext}"
    - name: 승인된 지원 묶음 적용
      oneport.support_apply:
        bundle: pinned-support-bundle
        scope: approved_namespace`
    : JSON.stringify(
        {
          preview: true,
          provider: s.target.kind,
          target: s.targetIdentity,
          authentication:
            s.target.kind === "aws"
              ? "AssumeRole + ExternalId"
              : "Workload Identity Federation",
          scopes: s.target.scopes,
          long_lived_keys: false,
        },
        null,
        2,
      );
  const material = {
    target: s.targetIdentity,
    context: s.selectedContext,
    definition,
    scopes: s.target.scopes,
  };
  const hash = await crypto.subtle.digest(
    "SHA-384",
    new TextEncoder().encode(JSON.stringify(material)),
  );
  return {
    id: crypto.randomUUID(),
    digest:
      "sha384:" +
      Array.from(new Uint8Array(hash), (b) =>
        b.toString(16).padStart(2, "0"),
      ).join(""),
    targetIdentity: s.targetIdentity!,
    expiresAt: Date.now() + 30 * 60_000,
    title: onprem
      ? "Kubernetes 지원 환경 준비"
      : `${s.target.kind === "aws" ? "AWS 역할 위임" : "Google Cloud 신원 연합"} 설정`,
    version: onprem ? "ansible-core 2.21 · Python 3.12" : "권한 계획 v1",
    definition,
    privileges: s.target.scopes,
    changes: onprem
      ? [
          "선택한 문맥에 전용 서비스 계정과 제한된 권한 구성",
          "기존 구성과 충돌하지 않는 지원 묶음만 설치",
          "만료되는 서비스 계정 토큰 사용",
        ]
      : [
          "선택한 자원 범위의 임시 자격 설정",
          "데이터·비밀 읽기와 복호화 권한을 개별 검증",
          "이관 생성·수정 권한 제외",
        ],
    restart: onprem
      ? "기존 업무 재시작 없음. 호스트 변경은 별도 계획이 필요해요."
      : "업무 재시작 없음",
    recovery:
      "현재 자원 상태를 대조한 뒤 완료한 작업을 제외해 재개해요. 결과가 불명확하면 멈춰요.",
    artifacts: [
      { name: "화면 예시 · 지원 묶음", digest: digest("a") },
      { name: "화면 예시 · 의존성 묶음", digest: digest("b") },
    ],
  };
}

export function createPreviewGateway(
  storage?: Pick<Storage, "getItem" | "setItem">,
) {
  let snapshots: ConnectionSnapshot[] = [];
  try {
    const saved: unknown = JSON.parse(storage?.getItem(STORAGE) ?? "[]");
    if (
      Array.isArray(saved) &&
      saved.length <= 20 &&
      saved.every(
        (s) =>
          typeof s.id === "string" &&
          typeof s.revision === "number" &&
          s.target &&
          Array.isArray(s.events) &&
          s.permissions,
      )
    )
      snapshots = saved;
  } catch {
    /* Invalid preview data is discarded. */
  }
  const listeners = new Set<() => void>();
  const emit = () => {
    try {
      storage?.setItem(STORAGE, JSON.stringify(snapshots));
    } catch {
      /* Preview works without persistence. */
    }
    listeners.forEach((l) => l());
  };
  const find = (id: string) => {
    const s = snapshots.find((s) => s.id === id);
    if (!s)
      throw new Error(
        "이 요청을 찾지 못했어요. 환경 목록에서 다시 선택하세요.",
      );
    return s;
  };
  const patch = (
    id: string,
    values: Partial<ConnectionSnapshot>,
    message?: string,
  ) => {
    const old = find(id),
      now = Date.now();
    const next = {
      ...old,
      ...values,
      revision: old.revision + 1,
      serverNow: now,
      observedAt: now,
      events: message
        ? [...old.events.slice(-19), { at: now, message }]
        : old.events,
    };
    snapshots = snapshots.map((s) => (s.id === id ? next : s));
    emit();
    return next;
  };
  const expire = () => {
    for (const s of snapshots) {
      if (s.registration === "ISSUED" && s.registrationExpiresAt <= Date.now())
        patch(s.id, { registration: "EXPIRED" }, "등록 기한이 끝났어요.");
      else if (
        s.device === "PENDING_APPROVAL" &&
        (s.approvalExpiresAt ?? 0) <= Date.now()
      )
        patch(
          s.id,
          {
            device: "REJECTED",
            issue: "단말 승인 기한이 끝났어요. 새 등록부터 시작하세요.",
          },
          "단말 승인 기한이 끝났어요.",
        );
      else if (
        (s.tunnel === "READY" || s.tunnel === "REKEYING") &&
        (s.keyExpiresAt ?? Infinity) <= Date.now()
      )
        patch(
          s.id,
          {
            tunnel: "DEGRADED",
            issue: "연결 키가 만료되어 작업을 차단했어요.",
          },
          "연결 키가 만료됐어요.",
        );
      else if (Date.now() - s.observedAt >= 5000) patch(s.id, {});
    }
  };
  const gateway: ConnectionGateway = {
    mode: "preview",
    available: true,
    list: () => snapshots,
    subscribe(listener) {
      listeners.add(listener);
      const timer = setInterval(expire, 1000);
      expire();
      return () => {
        listeners.delete(listener);
        clearInterval(timer);
      };
    },
    async create(target, publicKey) {
      if (snapshots.length >= 20)
        throw new Error(
          "미리보기 환경은 20개까지 만들 수 있어요. 브라우저의 미리보기 저장 자료를 초기화하세요.",
        );
      if (!targetValid(target) || !publicKey)
        throw new Error("환경 이름과 필수 항목을 확인하세요.");
      const now = Date.now();
      const s: ConnectionSnapshot = {
        id: crypto.randomUUID().replace(/-/g, "").slice(0, 22),
        revision: 1,
        target: structuredClone(target),
        serverNow: now,
        observedAt: now,
        registrationExpiresAt: now + 60_000,
        registration: "ISSUED",
        device: "NONE",
        tunnel: "NONE",
        planState: "NONE",
        job: "NONE",
        permissions: { approveDevice: true, approvePlan: true, revoke: true },
        attemptsLeft: 5,
        tunnelChecks: 0,
        contexts: [],
        jobs: [],
        epoch: 0,
        events: [
          {
            at: now,
            message:
              "미리보기 요청을 만들었어요. 실제 등록 요청은 전송하지 않았어요.",
          },
        ],
      };
      snapshots = [...snapshots, s];
      emit();
      return s;
    },
    async cancel(id) {
      const s = find(id);
      if (s.registration !== "ISSUED" && s.registration !== "EXPIRED")
        throw new Error(
          "단말이 등록된 요청은 단말 거절 또는 연결 해제로 종료하세요.",
        );
      snapshots = snapshots.filter((s) => s.id !== id);
      emit();
    },
    async approveDevice(id, candidateDigest, code) {
      const s = find(id);
      if (
        !canApproveDevice(s, Date.now()) ||
        s.candidate?.digest !== candidateDigest
      )
        throw new Error(
          "승인 대상이나 기한이 달라졌어요. 상태를 다시 확인하세요.",
        );
      if (normalizeCode(code) !== PREVIEW_CODE) {
        patch(id, {
          attemptsLeft: s.attemptsLeft - 1,
          ...(s.attemptsLeft === 1
            ? {
                device: "REJECTED" as const,
                issue:
                  "확인 코드 입력 횟수를 모두 사용했어요. 새 등록이 필요해요.",
              }
            : {}),
        });
        throw new Error(
          `확인 코드가 일치하지 않아요. ${s.attemptsLeft - 1}회 남았어요.`,
        );
      }
      patch(
        id,
        { device: "APPROVED", tunnel: "CONNECTING", issue: undefined },
        "단말을 승인했어요. 터널 준비만 허용돼요.",
      );
    },
    async reject(id) {
      if (find(id).device !== "PENDING_APPROVAL")
        throw new Error("현재 승인 대기 중인 단말이 아니에요.");
      patch(
        id,
        {
          device: "REJECTED",
          issue: "이 단말을 거절했어요. 다시 연결하려면 새 등록을 시작하세요.",
        },
        "단말 연결을 거절했어요.",
      );
    },
    async inspect(id, context) {
      const s = find(id);
      if (s.tunnel !== "READY" || s.device !== "APPROVED")
        throw new Error("터널을 먼저 확인해야 해요.");
      if (
        s.target.kind === "kubernetes" &&
        (!context || !s.contexts.includes(context))
      )
        throw new Error("확인할 Kubernetes 문맥을 선택하세요.");
      const identity =
        s.target.kind === "aws"
          ? "111122223333"
          : s.target.kind === "gcp"
            ? "oneport-demo-project"
            : `https://${context}.preview.invalid`;
      if (s.target.expectedIdentity && s.target.expectedIdentity !== identity)
        throw new Error(
          "예상 대상과 확인된 대상이 달라요. 환경 설정을 다시 검토하세요.",
        );
      const next = { ...s, selectedContext: context, targetIdentity: identity };
      const plan = await planFor(next);
      patch(
        id,
        {
          selectedContext: context,
          targetIdentity: identity,
          plan,
          planState: "PENDING",
        },
        "대상을 확인하고 검토용 계획을 만들었어요.",
      );
    },
    async approvePlan(id, hash) {
      const s = find(id);
      if (!canApprovePlan(s, Date.now()) || s.plan?.digest !== hash)
        throw new Error("계획이나 대상·기한이 달라졌어요. 다시 검토하세요.");
      patch(
        id,
        {
          planState: "APPROVED",
          job: "RUNNING",
          jobs: [
            {
              name: "승인한 범위 확인",
              state: "done",
              detail: "대상과 계획 해시 일치",
            },
            {
              name: "권한·지원 환경 적용",
              state: "running",
              detail: "승인한 작업만 실행",
            },
            {
              name: "환경 준비 검증",
              state: "waiting",
              detail: "자원·데이터·비밀 접근 범위 확인",
            },
          ],
        },
        "검토한 계획을 승인했어요. 예시 작업을 시작해요.",
      );
    },
    async reconnect(id) {
      const s = find(id);
      if (s.device !== "APPROVED" || s.tunnel !== "DEGRADED")
        throw new Error("이 연결은 재접속할 수 없어요.");
      patch(
        id,
        { tunnel: "CONNECTING", tunnelChecks: 0, issue: undefined },
        "이전 세대를 재사용하지 않고 새 연결을 준비해요.",
      );
    },
    async revoke(id) {
      const s = find(id);
      if (!s.permissions.revoke || s.device !== "APPROVED")
        throw new Error("연결을 해제할 권한이나 승인된 단말이 없어요.");
      patch(
        id,
        {
          device: "REVOKED",
          tunnel: "NONE",
          cleanup: {
            server: true,
            gateway: true,
            resources: false,
            local: false,
          },
        },
        "서버 권한과 터널을 차단했어요. 고객 권한과 단말 정리는 아직 미확인이에요.",
      );
    },
  };
  async function simulate(
    id: string,
    scenario: PreviewScenario | "execute" | "tunnel" | "complete",
  ) {
    const s = find(id);
    if (scenario === "execute" || scenario === "pending") {
      if (
        scenario === "execute" &&
        (s.registration !== "ISSUED" || s.registrationExpiresAt <= Date.now())
      )
        throw new Error("등록 기한이 끝났어요. 새 명령을 복사하세요.");
      return patch(
        id,
        {
          registration: "CONSUMED",
          device: "PENDING_APPROVAL",
          tunnel: "NONE",
          planState: "NONE",
          job: "NONE",
          plan: undefined,
          approvalExpiresAt: Date.now() + 300_000,
          attemptsLeft: 5,
          candidate: {
            id: "preview-candidate",
            digest: digest("c"),
            fingerprint: "91c7:3a54:eb02:6f18",
            hostname: "migration-connector",
            os: "Ubuntu 24.04 LTS",
            arch: "amd64",
          },
          issue: undefined,
        },
        "단말 등록을 재현했어요. 웹 확인이 필요해요.",
      );
    }
    if (scenario === "connecting") {
      await simulate(id, "pending");
      patch(id, { device: "APPROVED", tunnel: "CONNECTING" });
      return;
    }
    if (scenario === "tunnel") {
      if (s.device !== "APPROVED")
        throw new Error("단말 웹 승인이 먼저 필요해요.");
      return patch(
        id,
        {
          tunnel: "READY",
          tunnelChecks: 4,
          epoch: s.epoch + 1,
          keyExpiresAt: Date.now() + 30 * 60_000,
          contexts:
            s.target.kind === "kubernetes"
              ? ["busan-production", "busan-staging"]
              : [],
        },
        "보안터널 양방향 확인을 재현했어요.",
      );
    }
    if (scenario === "expired")
      return patch(id, {
        registration: "EXPIRED",
        device: "NONE",
        tunnel: "NONE",
        plan: undefined,
        job: "NONE",
        registrationExpiresAt: Date.now() - 1,
      });
    if (scenario === "blocked") {
      await simulate(id, "pending");
      return patch(id, {
        blockedUntil: Date.now() + 24 * 60 * 60_000,
        issue:
          "동일 출발지의 인증 실패가 하루 20회에 도달했어요. 서버의 차단 종료 시각 이후 다시 시도하세요.",
      });
    }
    if (
      scenario === "plan" ||
      scenario === "active" ||
      scenario === "rekeying" ||
      scenario === "degraded" ||
      scenario === "partial" ||
      scenario === "unknown" ||
      scenario === "revoked"
    ) {
      patch(id, {
        registration: "CONSUMED",
        device: "APPROVED",
        tunnel: "READY",
        tunnelChecks: 4,
        epoch: Math.max(1, s.epoch),
        keyExpiresAt: Date.now() + 30 * 60_000,
        contexts:
          s.target.kind === "kubernetes"
            ? ["busan-production", "busan-staging"]
            : [],
        blockedUntil: undefined,
        issue: undefined,
        planState: "NONE",
        job: "NONE",
      });
      // Scenario shortcuts deliberately bypass interactive prerequisites only in DEV.
      await gateway.inspect(
        id,
        s.target.kind === "kubernetes" ? "busan-production" : undefined,
      );
      if (scenario === "plan") return;
      await gateway.approvePlan(id, find(id).plan!.digest);
      if (scenario === "partial" || scenario === "unknown")
        return patch(id, {
          job: scenario === "partial" ? "PARTIAL_FAILURE" : "UNKNOWN_RESULT",
          jobs: find(id).jobs.map((j, i) => ({
            ...j,
            state:
              i === 0
                ? "done"
                : i === 1
                  ? scenario === "partial"
                    ? "failed"
                    : "unknown"
                  : "waiting",
          })),
          issue:
            scenario === "partial"
              ? "지원 환경 적용 일부가 실패했어요. 완료 단계를 반복하지 않고 실제 상태부터 확인해야 해요."
              : "변경 응답을 받지 못했어요. 실제 결과를 확인할 때까지 재실행할 수 없어요.",
        });
      await simulate(id, "complete");
      if (scenario === "degraded")
        return patch(id, {
          tunnel: "DEGRADED",
          issue:
            "터널 내부 통신을 확인하지 못해 작업을 차단했어요. 새 보안 연결이 필요해요.",
        });
      if (scenario === "rekeying")
        return patch(id, {
          tunnel: "REKEYING",
          issue:
            "새 키 세대를 적용하는 동안 작업을 잠시 멈춰요. 새 연결 확인 후 재개해요.",
        });
      if (scenario === "revoked") await gateway.revoke(id);
      return;
    }
    if (scenario === "complete") {
      if (s.job !== "RUNNING" || s.tunnel !== "READY")
        throw new Error("승인된 작업과 연결을 먼저 확인하세요.");
      return patch(
        id,
        {
          job: "SUCCEEDED",
          environmentId: "preview-env-" + id,
          verifiedAt: Date.now(),
          jobs: s.jobs.map((j) => ({ ...j, state: "done" as const })),
        },
        "예시 환경 준비를 검증했어요. 실제 이관을 수행한 결과가 아니에요.",
      );
    }
  }
  return { ...gateway, simulate };
}
