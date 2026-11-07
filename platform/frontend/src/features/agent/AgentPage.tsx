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
import {
  FilterSelect,
  PageHeader,
  ScopeFilterRow,
  Section,
  SummaryRow,
} from "../../components/PageKit";
import { Skeleton } from "../../components/QueryBoundary";
import { PanelGroup, SidePanel } from "../../components/SidePanel";
import { StatusDot } from "../../components/StatusDot";
import {
  formatAgo,
  formatDateTime,
  formatRatio,
  formatSeconds,
} from "../../lib/format";
import { fixView, hypotheses, sandbox } from "./evidence";
import { FixDiff, HypothesisTree, SandboxSteps } from "./RunEvidence";
import {
  agentScore,
  foldSteps,
  investigation,
  runSeconds,
  type Step,
  verdict,
} from "./investigation";
import {
  approvalModeLabel,
  changeText,
  DECISION_LABELS,
  filterRuns,
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
import { AgentSettings, RerunButton } from "./AgentSettings";
import { hasPendingRerun } from "./settings";
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
          <AgentStrip agents={status.agents} canCommand={status.can_command} />
          <ScoreRow runs={list.runs} />
          {status.agents.length > 0 && (
            <Section title="설정">
              <AgentSettings
                agents={status.agents}
                canCommand={status.can_command}
              />
            </Section>
          )}
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

function AgentStrip({
  agents,
  canCommand,
}: {
  agents: AgentStatus[];
  canCommand: boolean;
}) {
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
          {(canCommand || hasPendingRerun(agent.pending_commands)) && (
            <RerunButton
              agentId={agent.agent_id}
              pending={hasPendingRerun(agent.pending_commands)}
            />
          )}
        </li>
      ))}
    </ul>
  );
}

function ScoreRow({ runs }: { runs: AgentRun[] }) {
  const score = agentScore(runs);
  return (
    <Section title="AI 수정 성과" meta={`최근 ${runs.length}개 실행`}>
      <SummaryRow
        items={[
          {
            label: "감지한 문제",
            value: String(score.detected),
            unit: "건",
          },
          {
            label: "AI가 고침",
            value: String(score.resolved),
            unit: "건",
            tone: score.resolved > 0 ? "ok" : undefined,
            note:
              score.proposed > 0
                ? `제안 ${score.proposed}건 중 ${formatRatio(score.resolved, score.proposed)}`
                : "아직 제안이 없어요",
          },
          {
            label: "감지부터 복구까지",
            value: formatSeconds(score.medianSeconds),
            note: "중앙값",
          },
          {
            label: "사람이 봐야 함",
            value: String(score.handedOff + score.waiting),
            unit: "건",
            tone: score.waiting > 0 ? "warn" : undefined,
            note: `승인 대기 ${score.waiting}건, 넘김 ${score.handedOff}건`,
          },
        ]}
      />
    </Section>
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
            <th>걸린 시간</th>
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
                <td className="sub">{formatSeconds(runSeconds(run))}</td>
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
  const status = useQuery(agentStatusQueryOptions());
  const run = query.data;
  const agent = status.data?.agents.find((a) => a.agent_id === run?.agent_id);
  const rerunPending = agent ? hasPendingRerun(agent.pending_commands) : false;
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
      actions={
        run ? (
          <>
            <DecisionControls run={run} />
            {(run.can_rerun || rerunPending) && (
              <RerunButton agentId={run.agent_id} pending={rerunPending} />
            )}
          </>
        ) : undefined
      }
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
  const result = verdict(run);
  const took = runSeconds(run);
  const steps = investigation(run);
  const { shown, folded } = foldSteps(steps, took !== null);
  const detectStep = steps[0];
  const hyps = hypotheses(run.detail);
  const fix = fixView(run.detail);
  const box = sandbox(run.detail);
  return (
    <>
      <div className="agent-verdict" role="status">
        <StatusDot tone={result.tone}>{result.title}</StatusDot>
        {result.text && <p className="agent-text">{result.text}</p>}
        {took !== null && (
          <span className="sub">감지부터 끝까지 {formatSeconds(took)}</span>
        )}
      </div>
      {hyps.length > 0 && (
        <PanelGroup label="원인 가설" wide>
          <HypothesisTree
            items={hyps}
            trigger={detectStep.text}
            codes={detectStep.codes}
          />
        </PanelGroup>
      )}
      {fix && (
        <PanelGroup label="수정 내용" wide>
          <FixDiff fix={fix} />
        </PanelGroup>
      )}
      {box && (
        <PanelGroup label="샌드박스 작업" wide>
          <SandboxSteps box={box} />
        </PanelGroup>
      )}
      <PanelGroup label="조사 단계">
        <ol className="agent-steps">
          {shown.map((item) => (
            <StepItem key={item.key} item={item} />
          ))}
          {folded > 0 && (
            <li className="agent-step-skipped">
              <i className="dot dot-idle" aria-hidden="true" />
              <p className="agent-text">
                여기서 멈춰서 뒤의 {folded}단계는 진행하지 않았어요.
              </p>
            </li>
          )}
        </ol>
      </PanelGroup>
      <PanelGroup label="원본 기록">
        <details className="agent-raw">
          <summary>에이전트가 남긴 기록 {run.events.length}개</summary>
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
                  <span className="agent-timeline-message">
                    {event.message}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </details>
      </PanelGroup>
    </>
  );
}

function StepItem({ item }: { item: Step }) {
  return (
    <li className={item.reached ? undefined : "agent-step-skipped"}>
      <i className={`dot dot-${item.tone}`} aria-hidden="true" />
      <div className="agent-step-head">
        <span className="agent-step-title">{item.title}</span>
        {item.at && (
          <time className="sub" dateTime={item.at}>
            {formatDateTime(item.at)}
          </time>
        )}
      </div>
      <p className="agent-text">{item.text}</p>
      {item.codes.length > 0 && (
        <ul className="agent-codes">
          {item.codes.map((code) => (
            <li key={code} className="mono">
              {code}
            </li>
          ))}
        </ul>
      )}
      {item.notes.map((note) => (
        <p key={note.label} className="agent-step-note">
          <span className="sub">{note.label}</span> {note.text}
        </p>
      ))}
    </li>
  );
}
