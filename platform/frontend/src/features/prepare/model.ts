import type {
  ConnectionSnapshot,
  ConnectionTarget,
} from "./connectionTypes.ts";

export const PROVIDERS = [
  {
    value: "kubernetes",
    title: "온프레미스",
    detail: "기존 Kubernetes · kubectl",
    icon: "layers",
  },
  { value: "aws", title: "AWS", detail: "클라우드 계정 · aws", icon: "cloud" },
  {
    value: "gcp",
    title: "Google Cloud",
    detail: "클라우드 프로젝트 · gcloud",
    icon: "cloud",
  },
] as const;

export const SCOPES = [
  {
    value: "inventory.read",
    label: "자원 설정 조회",
    detail: "노드·네트워크·저장소와 구성 정보",
  },
  {
    value: "data.read",
    label: "저장 데이터 읽기",
    detail: "이관할 데이터의 내용",
  },
  {
    value: "secrets.read",
    label: "비밀 값 읽기",
    detail: "이관에 필요한 자격 정보의 값",
  },
  {
    value: "decrypt.use",
    label: "필요한 복호화",
    detail: "선택한 데이터와 비밀의 복호화 권한",
  },
] as const;

export const STEPS = [
  "환경 선택",
  "명령 실행",
  "단말 확인",
  "보안 연결",
  "계획 검토",
  "결과",
];
export const TUNNEL_CHECKS = [
  "상대 신원 확인",
  "양자내성 하이브리드 키 설정",
  "WireGuard 연결",
  "터널 내부 양방향 확인",
];

export function emptyTarget(team = ""): ConnectionTarget {
  return {
    organization: team,
    project: "",
    name: "",
    kind: "kubernetes",
    role: "source",
    region: "",
    expectedIdentity: "",
    scopes: ["inventory.read", "data.read", "secrets.read", "decrypt.use"],
  };
}

export function targetValid(target: ConnectionTarget) {
  return Boolean(
    target.organization.trim() &&
    target.project.trim() &&
    target.name.trim() &&
    target.scopes.includes("inventory.read") &&
    (target.kind === "kubernetes" || target.region.trim()),
  );
}

export function stepFor(s: ConnectionSnapshot | null, ready = false) {
  if (!s) return ready ? 1 : 0;
  if (s.registration === "ISSUED" || s.registration === "EXPIRED") return 1;
  if (s.device === "PENDING_APPROVAL" || s.device === "REJECTED") return 2;
  if (s.device === "REVOKED") return 5;
  if (s.tunnel !== "READY") return s.job === "SUCCEEDED" ? 5 : 3;
  if (s.job !== "NONE") return 5;
  return 4;
}

export function connectionStatus(s: ConnectionSnapshot): {
  label: string;
  tone: "ok" | "running" | "warn" | "bad" | "idle";
} {
  if (s.device === "REVOKED") return { label: "연결 해제됨", tone: "idle" };
  if (s.device === "REJECTED") return { label: "단말 승인 종료", tone: "idle" };
  if (s.registration === "EXPIRED")
    return { label: "등록 기한 종료", tone: "warn" };
  if (s.blockedUntil) return { label: "인증 일시 제한", tone: "bad" };
  if (s.tunnel === "DEGRADED") return { label: "연결 복구 필요", tone: "bad" };
  if (s.job === "PARTIAL_FAILURE")
    return { label: "일부 작업 실패", tone: "warn" };
  if (s.job === "UNKNOWN_RESULT")
    return { label: "결과 확인 필요", tone: "warn" };
  if (s.tunnel === "REKEYING")
    return { label: "연결 키 교체 중", tone: "running" };
  if (s.tunnel === "CONNECTING")
    return { label: "보안 연결 준비 중", tone: "running" };
  if (
    s.job === "SUCCEEDED" &&
    s.environmentId &&
    s.verifiedAt &&
    s.device === "APPROVED" &&
    s.tunnel === "READY"
  )
    return { label: "환경 준비 완료", tone: "ok" };
  if (s.job === "SUCCEEDED") return { label: "준비 검증 필요", tone: "idle" };
  return {
    label: [
      "환경 선택",
      "실행 대기",
      "단말 확인 필요",
      "보안 연결 준비 중",
      s.plan ? "계획 검토 필요" : "대상 확인 필요",
      "작업 진행 중",
    ][stepFor(s)],
    tone: "running",
  };
}

export function remainingSeconds(
  expiresAt: number | undefined,
  serverNow: number,
) {
  return expiresAt ? Math.max(0, Math.ceil((expiresAt - serverNow) / 1000)) : 0;
}
export function canApproveDevice(s: ConnectionSnapshot, now: number) {
  return (
    s.device === "PENDING_APPROVAL" &&
    s.permissions.approveDevice &&
    Boolean(s.candidate) &&
    remainingSeconds(s.approvalExpiresAt, now) > 0 &&
    s.attemptsLeft > 0 &&
    !s.blockedUntil
  );
}
export function canApprovePlan(s: ConnectionSnapshot, now: number) {
  return (
    s.device === "APPROVED" &&
    s.tunnel === "READY" &&
    s.planState === "PENDING" &&
    s.permissions.approvePlan &&
    remainingSeconds(s.plan?.expiresAt, now) > 0 &&
    s.plan?.targetIdentity === s.targetIdentity
  );
}
export function normalizeCode(value: string) {
  return value.toUpperCase().replace(/[\s-]/g, "");
}
export function codeValid(value: string) {
  return /^[A-HJ-NP-Z2-9]{8}$/.test(normalizeCode(value));
}
export function formatTime(value: number | undefined) {
  return value
    ? new Intl.DateTimeFormat("ko-KR", {
        timeZone: "Asia/Seoul",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(value)
    : "미확인";
}
