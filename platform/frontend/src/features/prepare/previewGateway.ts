import type {
  CollectedEnvironment,
  ConnectionGateway,
  ConnectionSnapshot,
  DiscoveredEnvironment,
} from "./connectionTypes.ts";
import {
  canApproveDevice,
  canRegister,
  normalizeCode,
  targetValid,
} from "./model.ts";
export const PREVIEW_CODE = "ABCD2345";
export const PREVIEW_SCENARIOS = [
  ["pending", "단말 확인 대기"],
  ["connecting", "보안 연결 중"],
  ["collected", "환경정보 수집 완료"],
  ["registered", "환경등록 완료"],
  ["expired", "명령 기한 종료"],
  ["rekeying", "키 교체"],
  ["degraded", "연결 장애"],
  ["blocked", "인증 제한"],
  ["revoked", "해제/정리 미확인"],
] as const;
export type PreviewScenario = (typeof PREVIEW_SCENARIOS)[number][0];
const STORAGE = "oneport.connection-preview.v2";
const sources: DiscoveredEnvironment[] = [
  {
    id: "onprem-busan",
    kind: "kubernetes",
    name: "busan-production",
    identity: "https://busan-production.preview.invalid",
    region: "",
    context: "busan-production",
  },
  {
    id: "aws-seoul",
    kind: "aws",
    name: "aws-seoul",
    identity: "111122223333",
    region: "ap-northeast-2",
  },
  {
    id: "gcp-seoul",
    kind: "gcp",
    name: "gcp-seoul",
    identity: "oneport-demo-project",
    region: "asia-northeast3",
  },
];
const facts = (source: DiscoveredEnvironment): CollectedEnvironment => ({
  ...source,
  facts:
    source.kind === "kubernetes"
      ? [
          { label: "Kubernetes", value: "v1.35.4, k3s" },
          { label: "노드", value: "3대, 12코어, 메모리 24GiB" },
          { label: "네임스페이스", value: "8개" },
          { label: "저장소", value: "local-path" },
        ]
      : [
          {
            label: "실행 도구",
            value:
              source.kind === "aws"
                ? "aws, 기존 로그인 확인"
                : "gcloud, 기존 로그인 확인",
          },
          {
            label: "클러스터",
            value: source.kind === "aws" ? "EKS 1개" : "GKE 1개",
          },
          { label: "네트워크", value: "가상 네트워크 2개" },
        ],
});

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
          typeof s?.id === "string" &&
          typeof s.organization === "string" &&
          typeof s.revision === "number" &&
          Array.isArray(s.discovered) &&
          Array.isArray(s.collected) &&
          Array.isArray(s.events) &&
          s.permissions,
      )
    )
      snapshots = saved;
  } catch {
    /* Discard invalid development data. */
  }
  const listeners = new Set<() => void>();
  const emit = () => {
    try {
      storage?.setItem(STORAGE, JSON.stringify(snapshots));
    } catch {
      /* Persistence is optional. */
    }
    listeners.forEach((l) => l());
  };
  const find = (id: string) => {
    const s = snapshots.find((s) => s.id === id);
    if (!s)
      throw new Error("수집 요청을 찾지 못했어요. 목록에서 다시 선택하세요.");
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
        patch(
          s.id,
          { registration: "EXPIRED" },
          "단말 등록 증명 접수 기한이 끝났어요.",
        );
      else if (
        s.device === "PENDING_APPROVAL" &&
        (s.approvalExpiresAt ?? 0) <= Date.now()
      )
        patch(s.id, {
          device: "REJECTED",
          issue: "단말 확인 기한이 끝났어요. 새 수집 요청을 시작하세요.",
        });
      else if (
        (s.tunnel === "READY" || s.tunnel === "REKEYING") &&
        (s.keyExpiresAt ?? Infinity) <= Date.now()
      )
        patch(s.id, {
          tunnel: "DEGRADED",
          issue: "연결 키가 만료되어 수집을 멈췄어요.",
        });
      else if (Date.now() - s.observedAt >= 5000) patch(s.id, {});
    }
  };
  const gateway: ConnectionGateway = {
    mode: "preview",
    available: true,
    providers: ["kubernetes", "aws", "gcp"],
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
    async create(context, publicKey) {
      if (snapshots.length >= 20)
        throw new Error("미리보기 요청은 20개까지 만들 수 있어요.");
      if (!context.organization.trim() || !publicKey)
        throw new Error("로그인한 조직과 등록 공개키를 확인하세요.");
      const now = Date.now();
      const s: ConnectionSnapshot = {
        id: crypto.randomUUID().replace(/-/g, "").slice(0, 22),
        revision: 1,
        organization: context.organization,
        provider: context.provider,
        target: null,
        serverNow: now,
        observedAt: now,
        registrationExpiresAt: now + 60_000,
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
        events: [
          {
            at: now,
            message:
              "화면 검토용 수집 요청을 만들었어요. 실제 단말 요청은 전송하지 않았어요.",
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
          "단말을 확인한 요청은 거절 또는 연결 해제로 종료하세요.",
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
        throw new Error("승인 대상이나 기한이 달라졌어요.");
      if (normalizeCode(code) !== PREVIEW_CODE) {
        patch(id, {
          attemptsLeft: s.attemptsLeft - 1,
          ...(s.attemptsLeft === 1
            ? {
                device: "REJECTED" as const,
                issue: "확인 코드를 5회 틀렸어요. 새 요청을 시작하세요.",
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
        "단말을 확인했어요. 보안 연결을 준비해요.",
      );
    },
    async reject(id) {
      if (find(id).device !== "PENDING_APPROVAL")
        throw new Error("확인 대기 중인 단말이 아니에요.");
      patch(
        id,
        {
          device: "REJECTED",
          issue: "단말을 거절했어요. 새 수집 요청을 시작하세요.",
        },
        "단말 확인을 거절했어요.",
      );
    },
    async rediscover(id) {
      const s = find(id);
      patch(id, {
        discoveryComplete: true,
        discovered: sources.filter(
          (candidate) => candidate.kind === s.provider,
        ),
        discoveryWarnings: [],
      });
    },
    async collect(id, discoveredId) {
      const s = find(id),
        source = s.discovered.find((e) => e.id === discoveredId);
      if (
        s.device !== "APPROVED" ||
        s.tunnel !== "READY" ||
        !source ||
        s.environmentId
      )
        throw new Error("보안 연결과 수집 대상을 다시 확인하세요.");
      const collected = [facts(source)],
        now = Date.now();
      const bytes = await crypto.subtle.digest(
        "SHA-384",
        new TextEncoder().encode(JSON.stringify({ collected, at: now })),
      );
      const current = find(id);
      if (
        current.device !== "APPROVED" ||
        current.tunnel !== "READY" ||
        current.epoch !== s.epoch ||
        current.environmentId
      )
        throw new Error("수집 중 연결 상태가 바뀌었어요. 다시 확인하세요.");
      patch(
        id,
        {
          collection: "COLLECTED",
          collected,
          collectedAt: now,
          inventoryDigest:
            "sha384:" +
            Array.from(new Uint8Array(bytes), (b) =>
              b.toString(16).padStart(2, "0"),
            ).join(""),
        },
        "선택한 대상의 정보 수집을 마쳤어요. 환경 이름을 등록하세요.",
      );
    },
    async register(id, collectedId, inventoryDigest, target) {
      const s = find(id),
        source = s.collected.find((e) => e.id === collectedId);
      if (
        !canRegister(s) ||
        !source ||
        s.inventoryDigest !== inventoryDigest ||
        !targetValid(target) ||
        target.organization !== s.organization ||
        target.kind !== source.kind ||
        target.region !== source.region
      )
        throw new Error(
          "수집 결과, 조직, 환경정보 또는 연결 상태가 달라졌어요. 다시 확인하세요.",
        );
      patch(
        id,
        {
          target: structuredClone(target),
          environmentId: "preview-env-" + id,
          selectedEnvironmentId: source.id,
          registeredAt: Date.now(),
        },
        "환경을 등록했어요. 마이그레이션 범위와 실행 계획은 앱 선택에서 검토해요.",
      );
    },
    async reconnect(id) {
      const s = find(id);
      if (s.device !== "APPROVED" || s.tunnel !== "DEGRADED")
        throw new Error("이 연결은 재접속할 수 없어요.");
      patch(
        id,
        { tunnel: "CONNECTING", tunnelChecks: 0, issue: undefined },
        "이전 키를 재사용하지 않고 새 보안 연결을 준비해요.",
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
        "서버 권한과 터널을 차단했어요. 고객 권한/단말 정리는 미확인이에요.",
      );
    },
  };
  async function simulate(
    id: string,
    scenario: PreviewScenario | "execute" | "tunnel",
  ) {
    const s = find(id);
    if (scenario === "execute" || scenario === "pending") {
      if (
        scenario === "execute" &&
        (s.registration !== "ISSUED" || s.registrationExpiresAt <= Date.now())
      )
        throw new Error("명령 기한이 끝났어요. 새 명령을 복사하세요.");
      return patch(
        id,
        {
          registration: "CONSUMED",
          device: "PENDING_APPROVAL",
          tunnel: "NONE",
          collection: "WAITING",
          discovered: [],
          collected: [],
          inventoryDigest: undefined,
          environmentId: undefined,
          target: null,
          registeredAt: undefined,
          approvalExpiresAt: Date.now() + 300_000,
          attemptsLeft: 5,
          blockedUntil: undefined,
          issue: undefined,
          candidate: {
            id: "preview-candidate",
            digest: "sha384:" + "c".repeat(96),
            fingerprint: "91c7:3a54:eb02:6f18",
            hostname: "migration-connector",
            os: "Ubuntu 24.04 LTS",
            arch: "amd64",
          },
        },
        "단말 등록을 재현했어요. 같은 단계에서 확인 코드를 입력하세요.",
      );
    }
    if (scenario === "connecting") {
      await simulate(id, "pending");
      patch(id, { device: "APPROVED", tunnel: "CONNECTING" });
      return;
    }
    if (scenario === "tunnel") {
      if (s.device !== "APPROVED")
        throw new Error("단말 웹 확인이 먼저 필요해요.");
      return patch(
        id,
        {
          tunnel: "READY",
          tunnelChecks: 4,
          epoch: s.epoch + 1,
          keyExpiresAt: Date.now() + 30 * 60_000,
          discovered: structuredClone(
            s.provider
              ? sources.filter((candidate) => candidate.kind === s.provider)
              : sources,
          ),
          discoveryComplete: true,
        },
        "보안 연결을 확인했어요. 읽을 계정이나 문맥을 선택하세요.",
      );
    }
    if (scenario === "expired")
      return patch(id, {
        registration: "EXPIRED",
        device: "NONE",
        tunnel: "NONE",
        collection: "WAITING",
        collected: [],
        target: null,
        environmentId: undefined,
        registrationExpiresAt: Date.now() - 1,
      });
    if (scenario === "blocked") {
      await simulate(id, "pending");
      return patch(id, {
        blockedUntil: Date.now() + 24 * 60 * 60_000,
        issue:
          "하루 20번째 인증 실패로 24시간 차단됐어요. 서버의 차단 종료 이후 다시 시도하세요.",
      });
    }
    await simulate(id, "connecting");
    await simulate(id, "tunnel");
    await gateway.collect(id, find(id).discovered[0].id);
    if (scenario === "collected") return;
    const current = find(id);
    await gateway.register(
      id,
      current.collected[0].id,
      current.inventoryDigest!,
      {
        organization: current.organization,
        project: "미리보기 프로젝트",
        name: current.collected[0].name,
        kind: current.collected[0].kind,
        region: current.collected[0].region,
      },
    );
    if (scenario === "registered") return;
    if (scenario === "degraded")
      return patch(id, {
        tunnel: "DEGRADED",
        issue:
          "보안 연결이 끊겼어요. 등록한 환경은 유지하며 재접속이 필요해요.",
      });
    if (scenario === "rekeying")
      return patch(id, {
        tunnel: "REKEYING",
        issue:
          "연결 키를 교체하고 있어요. 새 연결이 확인될 때까지 수집을 멈춰요.",
      });
    if (scenario === "revoked") await gateway.revoke(id);
  }
  return { ...gateway, simulate };
}
