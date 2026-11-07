import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type {
  CommandAction,
  CutoverCommand,
  MigrationDetail,
} from "../../api/migrations";
import { errorMessage } from "../../api/client";
import { Skeleton } from "../../components/QueryBoundary";
import { StatusDot } from "../../components/StatusDot";
import { formatDateTime } from "../../lib/format";
import {
  activeCommand,
  COMMAND_HELP,
  COMMAND_LABELS,
  COMMAND_STATUS_LABELS,
  commandResultText,
  commandTone,
  failedBeforeSwitch,
  PRIMARY_ACTIONS,
  visibleActions,
} from "./commands";
import { commandsQueryOptions, useRequestCommand } from "./queries";
import { pollInterval, stageTitle } from "./stages";

const ACTIVE_POLL_MS = 3000;
const APPROVE_WARNING =
  "승인하면 원본 쓰기를 차단하고 대상 DB 에 쓰기를 열어요. 쓰기 전환(SWITCHING) 뒤에는 자동으로 되돌릴 수 없어요.";

/**
 * 전환 제어 패널. 플랫폼은 명령을 저장만 하고, 온프렘 러너 Pod 의 보고기가 가져가 실행한다.
 * 대기·실행 중 명령이 있으면 모든 버튼을 잠근다(한 번에 한 명령).
 */
export function CutoverControls({
  jobId,
  detail,
  simulated,
}: {
  jobId: string;
  detail: MigrationDetail;
  simulated: boolean;
}) {
  const { summary } = detail;
  const commands = useQuery(
    commandsQueryOptions(jobId, pollInterval(summary.status)),
  );
  const active = activeCommand(commands.data);
  const polling = useQuery({
    ...commandsQueryOptions(jobId, ACTIVE_POLL_MS),
    enabled: active !== undefined,
  });
  void polling;
  const request = useRequestCommand(jobId);
  const [approving, setApproving] = useState(false);
  const [approvalId, setApprovalId] = useState("");
  const actions = visibleActions(
    summary.stage,
    failedBeforeSwitch(detail.stages),
  );
  const busy = active !== undefined || request.isPending;

  const send = (action: CommandAction, approval_id?: string) =>
    request.mutate(
      { action, approval_id },
      {
        onSuccess: () => {
          setApproving(false);
          setApprovalId("");
        },
      },
    );

  return (
    <div className="ctl">
      <p className="info-line">
        플랫폼은 명령을 저장만 해요. 온프렘 러너 Pod 의 보고기가 명령을 가져가
        cutover 도구를 실행하고 결과를 올려요. 클러스터와 DB 에 직접 쓰지
        않아요.
        {simulated &&
          " 시뮬레이션 데이터에서는 실행기가 없어 명령이 대기 상태로 남아요."}
      </p>
      {actions.length === 0 ? (
        <p className="empty-line">
          {stageTitle(summary.stage)} 단계에서는 보낼 명령이 없어요.
        </p>
      ) : (
        <div className="ctl-actions" role="group" aria-label="전환 명령">
          {actions.map((action) => (
            <button
              key={action}
              type="button"
              className={
                PRIMARY_ACTIONS.includes(action)
                  ? "pk-button"
                  : "pk-button weak"
              }
              title={COMMAND_HELP[action]}
              disabled={busy || (approving && action !== "approve")}
              aria-pressed={action === "approve" ? approving : undefined}
              onClick={() =>
                action === "approve" ? setApproving((v) => !v) : send(action)
              }
            >
              {COMMAND_LABELS[action]}
            </button>
          ))}
        </div>
      )}
      {approving && (
        <form
          className="ctl-approve"
          aria-label="전환 승인"
          onSubmit={(event) => {
            event.preventDefault();
            const id = approvalId.trim();
            if (id) send("approve", id);
          }}
        >
          <label>
            승인 ID
            <input
              type="text"
              name="approval_id"
              value={approvalId}
              autoComplete="off"
              placeholder="예: approve-forward-001"
              onChange={(event) => setApprovalId(event.target.value)}
              disabled={busy}
              required
            />
          </label>
          <p className="pk-alert">{APPROVE_WARNING}</p>
          <div className="ctl-approve-actions">
            <button
              type="submit"
              className="pk-button"
              disabled={busy || approvalId.trim() === ""}
            >
              승인 확정
            </button>
            <button
              type="button"
              className="pk-button weak"
              onClick={() => {
                setApproving(false);
                setApprovalId("");
              }}
            >
              취소
            </button>
          </div>
        </form>
      )}
      {request.isError && (
        <p className="pk-alert" role="alert">
          {errorMessage(request.error, "명령을 보내지 못했어요.")}
        </p>
      )}
      {active && (
        <p className="info-line" role="status">
          {COMMAND_LABELS[active.action]} 명령이{" "}
          {COMMAND_STATUS_LABELS[active.status]} 상태예요. 러너가 결과를 올릴
          때까지 다른 명령은 보낼 수 없어요.
        </p>
      )}
      <CommandList commands={commands.data} error={commands.isError} />
    </div>
  );
}

function CommandList({
  commands,
  error,
}: {
  commands: CutoverCommand[] | undefined;
  error: boolean;
}) {
  if (error)
    return (
      <p className="info-line" role="alert">
        명령 목록을 불러오지 못했어요. 자동으로 다시 시도해요.
      </p>
    );
  if (!commands) return <Skeleton rows={1} />;
  if (commands.length === 0)
    return <p className="empty-line">아직 보낸 명령이 없어요.</p>;
  return (
    <div className="table-wrap">
      <table className="pk-table">
        <thead>
          <tr>
            <th>요청</th>
            <th>명령</th>
            <th>상태</th>
            <th>요청자</th>
            <th>결과</th>
            <th>로그</th>
          </tr>
        </thead>
        <tbody>
          {commands.map((command) => (
            <tr key={command.id}>
              <td className="sub">
                <time dateTime={command.requested_at}>
                  {formatDateTime(command.requested_at)}
                </time>
              </td>
              <td>
                {COMMAND_LABELS[command.action]}
                {command.approval_id && (
                  <>
                    {" "}
                    <span className="mono sub">{command.approval_id}</span>
                  </>
                )}
                {command.note && <span className="tag">{command.note}</span>}
              </td>
              <td>
                <StatusDot tone={commandTone(command.status)}>
                  {COMMAND_STATUS_LABELS[command.status]}
                </StatusDot>
              </td>
              <td className="sub">{command.requested_by ?? "운영 화면"}</td>
              <td className="sub">{commandResultText(command)}</td>
              <td>
                {command.log_tail ? (
                  <details>
                    <summary>로그 끝부분</summary>
                    <pre className="ctl-log">{command.log_tail}</pre>
                  </details>
                ) : (
                  <span className="sub">-</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
