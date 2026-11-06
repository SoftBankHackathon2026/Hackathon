// 파드 표시 모델. 백엔드가 pods 를 보내면 파드별로, 아직이면 detail 의 "준비 x/y" 로 수만 그린다.
import type { ComponentPod, EnvComponent } from "../../api/migrations.ts";
import type { Tone } from "../migrations/stages.ts";
import { readiness } from "./insight.ts";

/** 대기/종료 사유를 짧은 한국어 + 원문으로. 모르는 사유는 원문 그대로. */
const REASON_LABELS: Record<string, string> = {
  CrashLoopBackOff: "재시작 반복",
  ImagePullBackOff: "이미지 받기 실패",
  ErrImagePull: "이미지 받기 실패",
  OOMKilled: "메모리 부족으로 종료",
  CreateContainerConfigError: "컨테이너 설정 오류",
  ContainerCreating: "컨테이너 만드는 중",
  Evicted: "노드에서 쫓겨남",
  Error: "오류로 종료",
  Completed: "완료",
};

export function reasonLabel(reason: string) {
  const label = REASON_LABELS[reason];
  return label ? `${label}(${reason})` : reason;
}

export function podTone(
  pod: Pick<ComponentPod, "phase" | "ready" | "reason">,
): Tone {
  if (pod.phase === "Failed") return "bad";
  if (
    pod.reason &&
    pod.reason !== "Completed" &&
    pod.reason !== "ContainerCreating"
  )
    return "bad";
  if (pod.phase === "Pending") return "warn";
  if (pod.phase === "Succeeded" || pod.phase === "Unknown") return "idle";
  return pod.ready ? "ok" : "warn";
}

export function podLabel(
  pod: Pick<ComponentPod, "phase" | "ready" | "reason">,
) {
  if (pod.reason && pod.reason !== "Completed") return reasonLabel(pod.reason);
  switch (pod.phase) {
    case "Pending":
      return "시작 중";
    case "Failed":
      return "실패";
    case "Succeeded":
      return "완료";
    case "Unknown":
      return "상태 정보 없음";
    default:
      return pod.ready ? "실행 중" : "준비 안 됨";
  }
}

export type PodSummary = {
  ready: number;
  total: number;
  restarts: number | null;
  /** pods: 파드별 정보 있음, detail: 준비 수만 있음 */
  source: "pods" | "detail";
};

/** 준비된 파드 수 / 전체. 관리형 서비스 등 파드가 없으면 null. */
export function podSummary(component: EnvComponent): PodSummary | null {
  if (component.pods && component.pods.length > 0)
    return {
      ready: component.pods.filter((p) => p.ready).length,
      total: component.pods.length,
      restarts: component.pods.reduce((n, p) => n + p.restarts, 0),
      source: "pods",
    };
  const r = readiness(component.detail).ready;
  if (!r) return null;
  return { ready: r.ready, total: r.total, restarts: null, source: "detail" };
}

/** 다이어그램에 그릴 파드 칸: 파드별 정보가 있으면 상태색, 없으면 준비 수만큼 정상 + 나머지 준비 안 됨. */
export function podCells(component: EnvComponent, max = 12) {
  const cells: { key: string; tone: Tone; title: string }[] =
    component.pods && component.pods.length > 0
      ? component.pods.map((pod) => ({
          key: pod.name,
          tone: podTone(pod),
          title: `${pod.name}, ${podLabel(pod)}`,
        }))
      : (() => {
          const s = podSummary(component);
          if (!s) return [];
          return Array.from({ length: s.total }, (_, i) => ({
            key: `${component.id}-${i}`,
            tone: (i < s.ready
              ? "ok"
              : component.status === "down"
                ? "bad"
                : "warn") as Tone,
            title: i < s.ready ? "준비됨" : "준비 안 됨",
          }));
        })();
  // 문제 있는 파드를 먼저 보여 준다.
  const order: Tone[] = ["bad", "warn", "running", "idle", "ok"];
  const sorted = [...cells].sort(
    (a, b) => order.indexOf(a.tone) - order.indexOf(b.tone),
  );
  return {
    cells: sorted.slice(0, max),
    hidden: Math.max(0, sorted.length - max),
  };
}

/** 시작 후 지난 시간(짧게). */
export function podAge(startedAt: string | null, now = Date.now()) {
  if (!startedAt) return null;
  const ms = now - new Date(startedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const m = Math.floor(ms / 60000);
  if (m < 60) return `${Math.max(1, m)}분`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}시간`;
  return `${Math.floor(h / 24)}일`;
}
