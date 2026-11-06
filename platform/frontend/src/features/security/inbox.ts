// 보안 문제 위쪽 필터 타일(Datadog Security Inbox 방식).
// 묶음 3개: 심각도(치명·높음) · 결과(실패·미확인) · 전환 영향(새로 실패·새로 통과).
// 타일 숫자는 고른 환경 범위의 통제 개수이고, 누르면 아래 목록에 그 조건을 건다.
import type { Posture } from "../../api/security.ts";
import {
  ALL_SEVERITIES,
  SHOWN_STATUSES,
  type ExplorerFilters,
} from "./explorer.ts";

export type TileId =
  "critical" | "high" | "fail" | "unknown" | "regressed" | "improved";

export function parityIds(posture: Posture, envIds: string[]) {
  const parities =
    posture.parities && posture.parities.length > 0
      ? posture.parities
      : [posture.parity];
  const scoped = parities.filter((p) => envIds.includes(p.target_env));
  return {
    regressed: [...new Set(scoped.flatMap((p) => p.regressions))],
    improved: [...new Set(scoped.flatMap((p) => p.improvements))],
    targets: scoped.map((p) => p.target_env),
  };
}

export function inboxCounts(posture: Posture, envIds: string[]) {
  const has = (status: string) =>
    posture.controls.filter((c) =>
      envIds.some((id) => c.results[id]?.status === status),
    );
  const failing = has("fail");
  const parity = parityIds(posture, envIds);
  return {
    critical: failing.filter((c) => c.severity === "critical").length,
    high: failing.filter((c) => c.severity === "high").length,
    fail: failing.length,
    unknown: has("unknown").length,
    regressed: parity.regressed.length,
    improved: parity.improved.length,
  } satisfies Record<TileId, number>;
}

/** 타일을 눌렀을 때 걸 필터. 환경·분류·표준·검색어는 그대로 둔다. */
export function tilePreset(
  tile: TileId,
  posture: Posture,
  envIds: string[],
): Partial<ExplorerFilters> {
  const parity = parityIds(posture, envIds);
  switch (tile) {
    case "critical":
    case "high":
      return { severity: [tile], status: ["fail"], ids: undefined };
    case "fail":
    case "unknown":
      return { severity: [...ALL_SEVERITIES], status: [tile], ids: undefined };
    case "regressed":
      return {
        severity: [...ALL_SEVERITIES],
        status: ["fail"],
        ids: parity.regressed,
      };
    case "improved":
      return {
        severity: [...ALL_SEVERITIES],
        status: [...SHOWN_STATUSES],
        ids: parity.improved,
      };
  }
}

export function activeTile(
  filters: ExplorerFilters,
  posture: Posture,
  envIds: string[],
): TileId | null {
  const same = (a: unknown[] | undefined, b: unknown[] | undefined) =>
    (a === undefined && b === undefined) ||
    (a !== undefined &&
      b !== undefined &&
      a.length === b.length &&
      a.every((v) => b.includes(v)));
  const tiles: TileId[] = [
    "critical",
    "high",
    "unknown",
    "regressed",
    "improved",
    "fail",
  ];
  return (
    tiles.find((tile) => {
      const preset = tilePreset(tile, posture, envIds);
      return (
        same(filters.severity, preset.severity) &&
        same(filters.status, preset.status) &&
        same(filters.ids, preset.ids)
      );
    }) ?? null
  );
}
