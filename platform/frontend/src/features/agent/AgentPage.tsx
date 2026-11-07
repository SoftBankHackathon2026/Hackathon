import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import type {
  AgentDecision,
  AgentRun,
  AgentRunDetail,
  AgentStatus,
} from "../../api/agent";
import { errorMessage } from "../../api/client";
import { agentHash, replaceHash } from "../../app/routes";
import { KeyValue } from "../../components/KeyValue";
import {
  FilterSelect,
  PageHeader,
  ScopeFilterRow,
  Section,
} from "../../components/PageKit";
import { Skeleton } from "../../components/QueryBoundary";
import { PanelGroup, SidePanel } from "../../components/SidePanel";
import { StatusDot } from "../../components/StatusDot";
import { formatAgo, formatDateTime } from "../../lib/format";
import {
  approvalModeLabel,
  changeRows,
  changeText,
  DECISION_LABELS,
  filterRuns,
  gateText,
  gateTone,
  gateView,
  proposalNotes,
  reasonText,
  runStateLabel,
  runTone,
  type RunFilter,
} from "./model";
import {
  agentRunQueryOptions,
  agentRunsQueryOptions,
  agentStatusQueryOptions,
  useDecideAgentRun,
} from "./queries";
import "./agent.css";

const CONFIRM_TEXT: Record<AgentDecision, string> = {
  approve:
    "승인하면 에이전트가 설정 저장소에 새 값을 쓰고 앱을 다시 시작해요. 적용 전에 값을 한 번 더 검사해요.",
  reject: "거절하면 이 제안을 적용하지 않고 실행을 끝내요.",
};

export function AgentPage({ runId }: { runId?: string }) {
  const { data: status } = useSuspenseQuery(agentStatusQueryOptions());
  const { data: list } = useSuspenseQuery(agentRunsQueryOptions());
  const [filter, setFilter] = useState<RunFilter>("acted");
  const rows = filterRuns(list.runs, filter);
  const select = useCallback(
    (id: string | undefined) => replaceHash(agentHash(id)),
    [],
  );
  const empty = status.agents.length === 0 && list.runs.length === 0;
  return (
    <div className="pk-page">
      <PageHeader title="AI 에이전트" />
      {empty ? (
        <p className="pk-empty">
          아직 보고한 에이전트가 없어요. 에이전트가 플랫폼 보고 토큰으로 신호를
          보내면 여기에 나타나요.
        </p>
      ) : (
        <>
          <AgentStrip agents={status.agents} />
          <ScopeFilterRow>
            <FilterSelect
              label="실행"
              value={filter}
              onChange={(value) => setFilter(value as RunFilter)}
              options={[
                { value: "acted", label: "조치한 실행" },
                { value: "all", label: "전체" },
              ]}
            />
          </ScopeFilterRow>
          <Section title="실행 기록" meta={rows.length}>
            <RunsTable rows={rows} selected={runId} onSelect={select} />
          </Section>
        </>
      )}
      {runId && (
        <RunPanel key={runId} runId={runId} onClose={() => select(undefined)} />
      )}
    </div>
  );
}

function AgentStrip({ agents }: { agents: AgentStatus[] }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(timer);
  }, []);
  if (agents.length === 0)
    return (
      <p className="info-line">
        아직 신호를 보낸 에이전트가 없어요. 실행 기록만 보여 드려요.
      </p>
    );
  return (
    <ul className="agent-strip" aria-label="에이전트 상태">
      {agents.map((agent) => (
        <li key={agent.agent_id}>
          <StatusDot tone={agent.online ? "ok" : "bad"}>
            {agent.online ? "연결됨" : "응답 없음"}
          </StatusDot>
          <span className="agent-strip-id mono">{agent.agent_id}</span>
          <span className="sub">v{agent.version}</span>
          <span className="sub">
            승인 모드 {approvalModeLabel(agent.auto_apply)}
          </span>
          <span className="sub" title={formatDateTime(agent.received_at)}>
            마지막 신호 {formatAgo(agent.received_at, now)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function RunsTable({
  rows,
  selected,
  onSelect,
}: {
  rows: AgentRun[];
  selected: string | undefined;
  onSelect: (id: string | undefined) => void;
}) {
  if (rows.length === 0)
    return <p className="empty-line">보여 줄 실행 기록이 없어요.</p>;
  return (
    <div className="table-wrap">
      <table className="pk-table" data-panel-owner>
        <thead>
          <tr>
            <th>상태</th>
            <th>시작</th>
            <th>작업</th>
            <th>판정 사유</th>
            <th>변경</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((run) => {
            const open = run.run_id === selected;
            const flip = () => onSelect(open ? undefined : run.run_id);
            return (
              <tr
                key={run.run_id}
                className={`clickable${open ? " selected" : ""}`}
                tabIndex={0}
                aria-selected={open}
                onClick={flip}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    flip();
                  }
                }}
              >
                <td>
                  <StatusDot tone={runTone(run.state)}>
                    {runStateLabel(run.state)}
                  </StatusDot>
                </td>
                <td className="sub">
                  <time dateTime={run.created_at}>
                    {formatDateTime(run.created_at)}
                  </time>
                </td>
                <td>
                  {run.job_id}
                  <span className="mono sub agent-run-id">{run.run_id}</span>
                </td>
                <td className="mono">{reasonText(run.summary.reason_codes)}</td>
                <td className="mono">{changeText(run.summary.changes)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RunPanel({ runId, onClose }: { runId: string; onClose: () => void }) {
  const query = useQuery(agentRunQueryOptions(runId));
  const run = query.data;
  return (
    <SidePanel
      kind="에이전트 실행"
      id={runId}
      onClose={onClose}
      title={run ? run.job_id : runId}
      facts={
        run
          ? [
              {
                label: "상태",
                value: (
                  <StatusDot tone={runTone(run.state)}>
                    {runStateLabel(run.state)}
                  </StatusDot>
                ),
              },
              { label: "에이전트", value: run.agent_id },
              { label: "시작", value: formatDateTime(run.created_at) },
              { label: "갱신", value: formatDateTime(run.updated_at) },
            ]
          : undefined
      }
      actions={run ? <DecisionControls run={run} /> : undefined}
    >
      {query.isError ? (
        <p className="pk-alert" role="alert">
          {errorMessage(query.error, "실행 기록을 불러오지 못했어요.")}
        </p>
      ) : !run ? (
        <Skeleton rows={4} />
      ) : (
        <RunBody run={run} />
      )}
    </SidePanel>
  );
}

function DecisionControls({ run }: { run: AgentRunDetail }) {
  const decide = useDecideAgentRun(run.run_id);
  const [confirming, setConfirming] = useState<AgentDecision | null>(null);
  const [sent, setSent] = useState<AgentDecision | null>(null);
  const pending = run.pending_approval;
  const waiting =
    pending !== null || (sent !== null && run.state === "WAITING_APPROVAL");
  const decision = pending?.decision ?? sent;
  return (
    <div className="agent-decide">
      {run.can_decide && !waiting && confirming === null && (
        <div className="ctl-actions" role="group" aria-label="승인 결정">
          <button
            type="button"
            className="pk-button"
            onClick={() => setConfirming("approve")}
          >
            승인
          </button>
          <button
            type="button"
            className="pk-button weak"
            onClick={() => setConfirming("reject")}
          >
            거절
          </button>
        </div>
      )}
      {run.can_decide && !waiting && confirming !== null && (
        <div className="agent-confirm" role="group" aria-label="결정 확인">
          <p className="pk-alert">{CONFIRM_TEXT[confirming]}</p>
          <div className="ctl-actions">
            <button
              type="button"
              className="pk-button"
              disabled={decide.isPending}
              onClick={() =>
                decide.mutate(confirming, {
                  onSuccess: () => {
                    setSent(confirming);
                    setConfirming(null);
                  },
                })
              }
            >
              {DECISION_LABELS[confirming]} 확정
            </button>
            <button
              type="button"
              className="pk-button weak"
              disabled={decide.isPending}
              onClick={() => setConfirming(null)}
            >
              취소
            </button>
          </div>
        </div>
      )}
      {waiting && decision && (
        <p className="info-line" role="status">
          {DECISION_LABELS[decision]} 요청을 보냈어요
          {pending ? ` (${pending.requested_by})` : ""}. 에이전트가 다음 주기에
          처리해요.
        </p>
      )}
      {decide.isError && (
        <p className="pk-alert" role="alert">
          {errorMessage(decide.error, "결정을 보내지 못했어요.")}
        </p>
      )}
    </div>
  );
}

function RunBody({ run }: { run: AgentRunDetail }) {
  const changes = changeRows(run.detail);
  const notes = proposalNotes(run.detail);
  const before = gateView(run.detail.pre_decision);
  const after = gateView(run.detail.post_decision);
  return (
    <>
      <PanelGroup label="진단">
        <p>{run.summary.diagnosis ?? "진단 기록이 없어요."}</p>
      </PanelGroup>
      <PanelGroup label="변경">
        {changes.length === 0 ? (
          <p>제안된 변경이 없어요.</p>
        ) : (
          <KeyValue
            rows={changes.map((change) => ({
              label: <span className="mono">{change.key}</span>,
              value: (
                <span className="mono">
                  {change.before !== null && (
                    <>
                      <span className="sub">{change.before}</span> →{" "}
                    </>
                  )}
                  {change.after}
                </span>
              ),
            }))}
          />
        )}
      </PanelGroup>
      <PanelGroup label="제안 근거">
        <KeyValue
          rows={[
            { label: "원인", value: notes.cause ?? "-" },
            { label: "영향", value: notes.impact ?? "-" },
            { label: "검증", value: notes.verification ?? "-" },
          ]}
        />
      </PanelGroup>
      <PanelGroup label="OPA 판정">
        <KeyValue
          rows={[
            {
              label: "적용 전",
              value: (
                <StatusDot tone={gateTone(before)}>
                  {gateText(before)}
                </StatusDot>
              ),
            },
            {
              label: "적용 후",
              value: (
                <StatusDot tone={gateTone(after)}>{gateText(after)}</StatusDot>
              ),
            },
          ]}
        />
      </PanelGroup>
      <PanelGroup label="타임라인">
        {run.events.length === 0 ? (
          <p>기록된 단계가 없어요.</p>
        ) : (
          <ol className="agent-timeline">
            {run.events.map((event, index) => (
              <li key={`${event.at}-${index}`}>
                <i
                  className={`dot dot-${runTone(event.state)}`}
                  aria-hidden="true"
                />
                <time dateTime={event.at}>{formatDateTime(event.at)}</time>
                <span className="agent-timeline-state">
                  {runStateLabel(event.state)}
                </span>
                <span className="agent-timeline-message">{event.message}</span>
              </li>
            ))}
          </ol>
        )}
      </PanelGroup>
    </>
  );
}
