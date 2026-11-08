import { useState } from "react";
import type { MigrationSummary } from "../../api/migrations";
import { errorMessage } from "../../api/client";
import { replaceHash } from "../../app/routes";
import { carryNotice, confirmText, deletionText } from "./deletion";
import { useDeleteMigrations } from "./queries";

export type OnDeleted = (ids: string[], text: string) => void;

/** 작업 표 위 한 줄: 고른 수, 선택 삭제, 확인 한 번, 결과 문장. */
export function TaskDeleteBar({
  selectable,
  jobIds,
  notice,
  onDeleted,
}: {
  selectable: boolean;
  jobIds: string[];
  notice: string | null;
  onDeleted: OnDeleted;
}) {
  const [confirming, setConfirming] = useState(false);
  const remove = useDeleteMigrations((result) => {
    setConfirming(false);
    onDeleted(result.deleted, deletionText(result));
  });
  const count = jobIds.length;
  const asking = confirming && count > 0;
  return (
    <div className="task-bulk">
      {asking ? (
        <DeleteConfirm
          count={count}
          pending={remove.isPending}
          onCancel={() => setConfirming(false)}
          onConfirm={() => remove.mutate(jobIds)}
        />
      ) : (
        selectable && (
          <div className="ctl-actions" role="group" aria-label="선택한 작업">
            <span className="task-bulk-count">{count}개 선택</span>
            <button
              type="button"
              className="pk-button weak"
              disabled={count === 0}
              onClick={() => setConfirming(true)}
            >
              선택 삭제
            </button>
          </div>
        )
      )}
      {notice && !asking && (
        <p className="info-line" role="status">
          {notice}
        </p>
      )}
      {remove.isError && (
        <p className="pk-alert" role="alert">
          {errorMessage(remove.error, "작업을 지우지 못했어요.")}
        </p>
      )}
    </div>
  );
}

function DeleteConfirm({
  count,
  pending,
  onConfirm,
  onCancel,
}: {
  count: number;
  pending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="task-confirm" role="group" aria-label="삭제 확인">
      <p className="info-line">{confirmText(count)}</p>
      <div className="ctl-actions">
        <button
          type="button"
          className="pk-button"
          disabled={pending}
          onClick={onConfirm}
        >
          삭제 확정
        </button>
        <button
          type="button"
          className="pk-button weak"
          disabled={pending}
          onClick={onCancel}
        >
          취소
        </button>
      </div>
    </div>
  );
}

/** 작업 상세의 삭제 확인. 지우면 개요로 돌아가 결과 문장을 보이고, 건너뛰면 여기서 사유를 알린다. */
export function TaskDeletePanel({
  summary,
  onCancel,
}: {
  summary: MigrationSummary;
  onCancel: () => void;
}) {
  const [kept, setKept] = useState<string | null>(null);
  const remove = useDeleteMigrations((result) => {
    const text = deletionText(result);
    if (result.deleted.length > 0) {
      carryNotice(text);
      replaceHash("#migrations");
    } else setKept(text);
  });
  return (
    <div className="task-detail-delete">
      {kept ? (
        <p className="info-line" role="status">
          {kept}
        </p>
      ) : (
        <DeleteConfirm
          count={1}
          pending={remove.isPending}
          onCancel={onCancel}
          onConfirm={() => remove.mutate([summary.job_id])}
        />
      )}
      {remove.isError && (
        <p className="pk-alert" role="alert">
          {errorMessage(remove.error, "작업을 지우지 못했어요.")}
        </p>
      )}
    </div>
  );
}
