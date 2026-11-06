import type {
  ConnectionSnapshot,
  ConnectionTarget,
} from "./connectionTypes.ts";
export const PROVIDERS = [
  {
    value: "kubernetes",
    title: "온프레미스",
    detail: "기존 Kubernetes",
    icon: "layers",
  },
  { value: "aws", title: "AWS", detail: "클라우드 계정", icon: "cloud" },
  {
    value: "gcp",
    title: "Google Cloud",
    detail: "클라우드 프로젝트",
    icon: "cloud",
  },
] as const;
export const STEPS = ["환경정보 수집", "환경등록"];
export const TUNNEL_CHECKS = [
  "상대 신원 확인",
  "양자내성 하이브리드 키 설정",
  "WireGuard 연결",
  "터널 내부 양방향 확인",
];
export function emptyTarget(organization = ""): ConnectionTarget {
  return {
    organization,
    project: "",
    name: "",
    kind: "kubernetes",
    role: "source",
    region: "",
  };
}
export function targetValid(target: ConnectionTarget) {
  return Boolean(
    target.organization.trim() &&
    target.project.trim() &&
    target.name.trim() &&
    (target.role === "source" || target.role === "target") &&
    (target.kind === "kubernetes" || target.region.trim()),
  );
}
export function stepFor(s: ConnectionSnapshot | null) {
  if (s?.environmentId) return 1;
  return s?.device === "APPROVED" &&
    s.tunnel === "READY" &&
    s.collection === "COLLECTED" &&
    s.collected.length > 0
    ? 1
    : 0;
}
export function connectionStatus(s: ConnectionSnapshot): {
  label: string;
  tone: "ok" | "running" | "warn" | "bad" | "idle";
} {
  if (s.device === "REVOKED") return { label: "연결 해제됨", tone: "idle" };
  if (s.device === "REJECTED") return { label: "단말 확인 종료", tone: "idle" };
  if (s.registration === "EXPIRED")
    return { label: "명령 기한 종료", tone: "warn" };
  if (s.blockedUntil) return { label: "인증 일시 제한", tone: "bad" };
  if (s.tunnel === "DEGRADED") return { label: "연결 복구 필요", tone: "bad" };
  if (s.tunnel === "REKEYING")
    return { label: "연결 키 교체 중", tone: "running" };
  if (s.tunnel === "CONNECTING")
    return { label: "보안 연결 중", tone: "running" };
  if (
    s.environmentId &&
    s.target &&
    s.registeredAt &&
    s.device === "APPROVED" &&
    s.tunnel === "READY"
  )
    return { label: "환경등록 완료", tone: "ok" };
  if (s.device === "PENDING_APPROVAL")
    return { label: "단말 확인 필요", tone: "running" };
  if (s.collection === "COLLECTED")
    return { label: "환경등록 필요", tone: "running" };
  return {
    label: s.registration === "ISSUED" ? "명령 실행 대기" : "환경정보 수집 중",
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
export function canRegister(s: ConnectionSnapshot) {
  return (
    s.device === "APPROVED" &&
    s.tunnel === "READY" &&
    s.collection === "COLLECTED" &&
    s.collected.length > 0 &&
    Boolean(s.inventoryDigest) &&
    s.permissions.registerEnvironment &&
    !s.environmentId &&
    !s.blockedUntil
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
