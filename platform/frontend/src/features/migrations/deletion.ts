import type {
  Fleet,
  MigrationSummary,
  SkippedTask,
  TaskDeletion,
} from "../../api/migrations.ts";

/**
 * 마이그레이션 작업 지우기: 표 선택과 결과 문장. 화면 상태는 Set 으로 들고, 보이는 행 기준으로 읽는다.
 * 지울 수 있는지는 서버가 정한다(등록한 작업이고, 진행 중이 아니고, 권한이 있을 때 can_delete).
 */

export type HeaderCheck = "none" | "some" | "all";
/** 행 앞 칸: 고를 수 있음, 진행 중이라 잠김, 설정 파일 작업이라 칸 없음. */
export type RowCheck = "open" | "locked" | "none";

export const LOCKED_TITLE = "진행 중인 작업은 지울 수 없어요.";

/** 선택 칸은 권한이 있고 보이는 행에 등록한 작업이 하나라도 있을 때만 둔다. */
export function showsChecks(
  fleet: Pick<Fleet, "can_delete">,
  rows: MigrationSummary[],
) {
  return fleet.can_delete && rows.some((job) => job.registered);
}

export function rowCheck(job: MigrationSummary): RowCheck {
  if (!job.registered) return "none";
  return job.can_delete ? "open" : "locked";
}

export function deletableIds(rows: MigrationSummary[]) {
  return rows.filter((job) => job.can_delete).map((job) => job.job_id);
}

/** 지금 보이고 지울 수 있는 행 중 고른 것만, 표 순서대로. 필터를 바꾸거나 행이 사라지면 빠진다. */
export function chosenIds(
  selected: ReadonlySet<string>,
  rows: MigrationSummary[],
) {
  return deletableIds(rows).filter((id) => selected.has(id));
}

export function toggleTask(selected: ReadonlySet<string>, jobId: string) {
  const next = new Set(selected);
  if (next.has(jobId)) next.delete(jobId);
  else next.add(jobId);
  return next;
}

/** 머리 체크 상자: 보이는 지울 수 있는 행을 모두 골랐으면 모두 풀고, 아니면 모두 고른다. */
export function toggleAll(
  selected: ReadonlySet<string>,
  rows: MigrationSummary[],
) {
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
  rows: MigrationSummary[],
): HeaderCheck {
  const ids = deletableIds(rows);
  const count = ids.filter((id) => selected.has(id)).length;
  if (count === 0) return "none";
  return count === ids.length ? "all" : "some";
}

export function confirmText(count: number) {
  return `작업 ${count}개를 지워요. 목록과 상세 화면에서 사라지고 되돌릴 수 없어요.`;
}

/** 건너뛴 작업을 사유별로 묶어 한 문장으로. 없으면 null. */
export function skippedText(skipped: SkippedTask[]) {
  if (skipped.length === 0) return null;
  const counts = new Map<string, number>();
  for (const item of skipped)
    counts.set(item.reason, (counts.get(item.reason) ?? 0) + 1);
  const parts = [...counts].map(([reason, count]) => `${reason} ${count}개`);
  return `${parts.join(", ")}는 지우지 않았어요.`;
}

export function deletionText(result: TaskDeletion) {
  const done =
    result.deleted.length > 0
      ? `작업 ${result.deleted.length}개를 지웠어요.`
      : null;
  return [done, skippedText(result.skipped)].filter(Boolean).join(" ");
}

/** 지운 작업을 바로 목록에서 뺀다. 다시 읽기 전 잠깐 지운 행이 남거나 상세를 다시 부르지 않게. */
export function withoutDeleted(fleet: Fleet, deleted: string[]): Fleet {
  if (deleted.length === 0) return fleet;
  const gone = new Set(deleted);
  return {
    ...fleet,
    migrations: fleet.migrations.filter((job) => !gone.has(job.job_id)),
  };
}

/**
 * 상세 화면에서 지우고 개요로 돌아갈 때 결과 문장을 넘긴다. 개요는 처음 그릴 때 읽고(StrictMode 의
 * 두 번 호출에도 같은 값), 그린 뒤 비운다.
 */
let carried: string | null = null;

export function carryNotice(text: string) {
  carried = text;
}

export function carriedNotice() {
  return carried;
}

export function dropNotice() {
  carried = null;
}
