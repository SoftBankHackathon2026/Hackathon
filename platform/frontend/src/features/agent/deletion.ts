import type {
  AgentRun,
  AgentRunDeletion,
  AgentSkippedRun,
} from "../../api/agent.ts";

/** 실행 기록 지우기: 표 선택과 결과 문장. 화면 상태는 Set 으로 들고, 보이는 행 기준으로 읽는다. */

export type HeaderCheck = "none" | "some" | "all";

export function deletableIds(rows: AgentRun[]) {
  return rows.filter((run) => run.can_delete).map((run) => run.run_id);
}

/** 지금 보이고 지울 수 있는 행 중 고른 것만, 표 순서대로. 필터를 바꾸거나 행이 사라지면 빠진다. */
export function chosenIds(selected: ReadonlySet<string>, rows: AgentRun[]) {
  return deletableIds(rows).filter((id) => selected.has(id));
}

export function toggleRun(selected: ReadonlySet<string>, runId: string) {
  const next = new Set(selected);
  if (next.has(runId)) next.delete(runId);
  else next.add(runId);
  return next;
}

/** 머리 체크 상자: 보이는 지울 수 있는 행을 모두 골랐으면 모두 풀고, 아니면 모두 고른다. */
export function toggleAll(selected: ReadonlySet<string>, rows: AgentRun[]) {
  const ids = deletableIds(rows);
  const next = new Set(selected);
  const all = ids.length > 0 && ids.every((id) => next.has(id));
  for (const id of ids) {
    if (all) next.delete(id);
    else next.add(id);
  }
  return next;
}

export function headerCheck(
  selected: ReadonlySet<string>,
  rows: AgentRun[],
): HeaderCheck {
  const ids = deletableIds(rows);
  const count = ids.filter((id) => selected.has(id)).length;
  if (count === 0) return "none";
  return count === ids.length ? "all" : "some";
}

export function confirmText(count: number) {
  return `기록 ${count}개를 지워요. 에이전트가 다시 보내도 목록에 나오지 않아요.`;
}

/** 건너뛴 실행을 사유별로 묶어 한 문장으로. 없으면 null. */
export function skippedText(skipped: AgentSkippedRun[]) {
  if (skipped.length === 0) return null;
  const counts = new Map<string, number>();
  for (const item of skipped)
    counts.set(item.reason, (counts.get(item.reason) ?? 0) + 1);
  const parts = [...counts].map(([reason, count]) => `${reason} ${count}개`);
  return `${parts.join(", ")}는 지우지 않았어요.`;
}

export function deletionText(result: AgentRunDeletion) {
  const done =
    result.deleted.length > 0
      ? `기록 ${result.deleted.length}개를 지웠어요.`
      : null;
  return [done, skippedText(result.skipped)].filter(Boolean).join(" ");
}
