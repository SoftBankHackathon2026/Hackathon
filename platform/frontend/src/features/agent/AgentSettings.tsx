import { useState, type ReactNode } from "react";
import type { AgentSettingsChange, AgentStatus } from "../../api/agent";
import { errorMessage } from "../../api/client";
import { Segmented } from "../../components/Segmented";
import { formatAgo, formatDateTime } from "../../lib/format";
import { approvalModeLabel } from "./model";
import { useChangeAgentSettings, useRerunAgent } from "./queries";
import {
  changeLines,
  commandText,
  draftError,
  draftFrom,
  INTERVAL_MAX,
  INTERVAL_MIN,
  onOffLabel,
  readConfig,
  settingsChange,
  type ConfigView,
  type SettingsDraft,
} from "./settings";

const AUTO_APPLY_WARNING =
  "자동 적용으로 바꾸면 AI 가 사람 승인 없이 설정 저장소에 값을 쓰고 앱을 다시 시작해요.";

export function RerunButton({
  agentId,
  pending,
}: {
  agentId: string;
  pending: boolean;
}) {
  const rerun = useRerunAgent(agentId);
  if (pending)
    return (
      <p className="info-line agent-note" role="status">
        다시 판정 요청을 보냈어요. 에이전트가 다음 주기에 처리해요.
      </p>
    );
  return (
    <span className="agent-rerun">
      <button
        type="button"
        className="pk-button weak"
        disabled={rerun.isPending}
        onClick={() => rerun.mutate()}
      >
        지금 다시 판정
      </button>
      {rerun.isError && (
        <span className="pk-alert" role="alert">
          {errorMessage(rerun.error, "요청을 보내지 못했어요.")}
        </span>
      )}
    </span>
  );
}

export function AgentSettings({
  agents,
  canCommand,
}: {
  agents: AgentStatus[];
  canCommand: boolean;
}) {
  if (agents.length === 0) return null;
  return (
    <div className="agent-settings-list">
      {agents.map((agent) => (
        <SettingsCard
          key={agent.agent_id}
          agent={agent}
          canCommand={canCommand}
          titled={agents.length > 1}
        />
      ))}
    </div>
  );
}

function SettingsCard({
  agent,
  canCommand,
  titled,
}: {
  agent: AgentStatus;
  canCommand: boolean;
  titled: boolean;
}) {
  const config = readConfig(agent.config);
  const [editing, setEditing] = useState(false);
  const [sent, setSent] = useState(false);
  const pending = agent.pending_commands;
  return (
    <section className="agent-settings" aria-label={`${agent.agent_id} 설정`}>
      {titled && <h3 className="mono">{agent.agent_id}</h3>}
      {config === null ? (
        <p className="empty-line">에이전트가 아직 설정을 보내지 않았어요.</p>
      ) : editing ? (
        <SettingsForm
          agentId={agent.agent_id}
          config={config}
          onDone={(saved) => {
            setEditing(false);
            if (saved) setSent(true);
          }}
        />
      ) : (
        <>
          <ConfigFacts config={config} />
          {canCommand && (
            <div className="ctl-actions">
              <button
                type="button"
                className="pk-button weak"
                onClick={() => {
                  setSent(false);
                  setEditing(true);
                }}
              >
                설정 바꾸기
              </button>
            </div>
          )}
        </>
      )}
      {sent && pending.length === 0 && (
        <p className="info-line agent-note" role="status">
          에이전트가 다음 주기에 반영해요.
        </p>
      )}
      {pending.length > 0 && (
        <div className="agent-pending" role="status">
          <p className="info-line agent-note">
            에이전트가 다음 주기에 반영해요. 아직 처리하지 않은 요청이에요.
          </p>
          <ul>
            {pending.map((command) => (
              <li key={command.id}>
                <span className="agent-text">{commandText(command)}</span>
                <span
                  className="sub"
                  title={formatDateTime(command.requested_at)}
                >
                  {command.requested_by}, {formatAgo(command.requested_at)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function ConfigFacts({ config }: { config: ConfigView }) {
  return (
    <dl className="agent-facts">
      <Fact label="승인 모드">
        {config.autoApply === null ? "-" : approvalModeLabel(config.autoApply)}
      </Fact>
      <Fact label="판정 주기">
        {config.intervalSeconds === null ? "-" : `${config.intervalSeconds}초`}
      </Fact>
      <Fact label="Slack">
        <span className="agent-text">
          {config.slack.mode}, 알림 {onOffLabel(config.slack.enabled)}
        </span>
        <span className="agent-text sub">
          채널 <span className="mono">{config.slack.channel ?? "-"}</span>
        </span>
        <span className="agent-text sub">
          스레드 ts{" "}
          <span className="mono">
            {config.slack.threadTs ?? "없음 (채널에 바로)"}
          </span>
        </span>
      </Fact>
      <Fact label="고칠 수 있는 키">
        {config.fixKeys.length === 0 ? (
          "-"
        ) : (
          <ul className="agent-codes">
            {config.fixKeys.map((key) => (
              <li key={key} className="mono">
                {key}
              </li>
            ))}
          </ul>
        )}
      </Fact>
      <Fact label="계획서 값">
        {config.plan.length === 0 ? (
          "-"
        ) : (
          <ul className="agent-plan">
            {config.plan.map((entry) => (
              <li key={entry.key} className="mono">
                {entry.key}
                <span className="agent-arrow" aria-hidden="true">
                  →
                </span>
                {entry.value}
              </li>
            ))}
          </ul>
        )}
      </Fact>
      <Fact label="샌드박스">
        <span className="agent-text">
          실행 방식 <span className="mono">{config.sandbox.runner ?? "-"}</span>
        </span>
        <span className="agent-text sub">
          이미지 <span className="mono">{config.sandbox.image ?? "-"}</span>
        </span>
        <span className="agent-text sub">
          모델 <span className="mono">{config.sandbox.model ?? "-"}</span>
        </span>
      </Fact>
      <Fact label="마지막 설정 반영">
        {config.appliedAt === null ? "-" : formatDateTime(config.appliedAt)}
      </Fact>
    </dl>
  );
}

function SettingsForm({
  agentId,
  config,
  onDone,
}: {
  agentId: string;
  config: ConfigView;
  onDone: (saved: boolean) => void;
}) {
  const base = draftFrom(config);
  const [draft, setDraft] = useState<SettingsDraft>(base);
  const [confirming, setConfirming] = useState<AgentSettingsChange | null>(
    null,
  );
  const save = useChangeAgentSettings(agentId);
  const error = draftError(draft);
  const change = settingsChange(base, draft);
  const changed = Object.keys(change).length > 0;
  const set = (patch: Partial<SettingsDraft>) => {
    setConfirming(null);
    setDraft((current) => ({ ...current, ...patch }));
  };
  return (
    <form
      className="agent-form"
      aria-label="설정 바꾸기"
      onSubmit={(event) => {
        event.preventDefault();
        if (!error && changed) setConfirming(change);
      }}
    >
      <div className="agent-field">
        <span>승인 모드</span>
        <Segmented
          label="승인 모드"
          value={draft.autoApply ? "auto" : "human"}
          options={[
            { value: "human", label: "사람 승인" },
            { value: "auto", label: "자동 적용" },
          ]}
          onChange={(value) => set({ autoApply: value === "auto" })}
        />
      </div>
      {draft.autoApply && !base.autoApply && (
        <p className="pk-alert">{AUTO_APPLY_WARNING}</p>
      )}
      <label className="agent-field">
        <span>판정 주기 (초)</span>
        <input
          type="number"
          inputMode="numeric"
          min={INTERVAL_MIN}
          max={INTERVAL_MAX}
          step={1}
          value={draft.interval}
          onChange={(event) => set({ interval: event.target.value })}
        />
      </label>
      <div className="agent-field">
        <span>Slack 알림</span>
        <Segmented
          label="Slack 알림"
          value={draft.slackEnabled ? "on" : "off"}
          options={[
            { value: "on", label: "켜짐" },
            { value: "off", label: "꺼짐" },
          ]}
          onChange={(value) => set({ slackEnabled: value === "on" })}
        />
      </div>
      <label className="agent-field">
        <span>스레드 ts</span>
        <input
          type="text"
          inputMode="decimal"
          autoComplete="off"
          placeholder="비우면 스레드 없이 채널에 바로"
          value={draft.threadTs}
          onChange={(event) => set({ threadTs: event.target.value })}
        />
      </label>
      {error && <p className="pk-alert">{error}</p>}
      {confirming ? (
        <div className="agent-confirm" role="group" aria-label="설정 확인">
          <p className="info-line">이렇게 바꿀게요.</p>
          <ul className="agent-change-list">
            {changeLines(confirming).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <div className="ctl-actions">
            <button
              type="button"
              className="pk-button"
              disabled={save.isPending}
              onClick={() =>
                save.mutate(confirming, { onSuccess: () => onDone(true) })
              }
            >
              바꾸기 확정
            </button>
            <button
              type="button"
              className="pk-button weak"
              disabled={save.isPending}
              onClick={() => setConfirming(null)}
            >
              다시 고치기
            </button>
          </div>
        </div>
      ) : (
        <div className="ctl-actions">
          <button
            type="submit"
            className="pk-button"
            disabled={Boolean(error) || !changed}
          >
            저장
          </button>
          <button
            type="button"
            className="pk-button weak"
            onClick={() => onDone(false)}
          >
            취소
          </button>
        </div>
      )}
      {save.isError && (
        <p className="pk-alert" role="alert">
          {errorMessage(save.error, "설정을 보내지 못했어요.")}
        </p>
      )}
    </form>
  );
}
