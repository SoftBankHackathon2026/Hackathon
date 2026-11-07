import { request } from "./client";

export type AgentRunState =
  | "NOTHING_TO_FIX"
  | "CHECKING"
  | "WAITING_APPROVAL"
  | "APPLYING"
  | "RESOLVED"
  | "UNRESOLVED"
  | "HANDED_OFF"
  | "REJECTED"
  | "PROBE_FAILED"
  | "CAS_CONFLICT"
  | "RESTART_FAILED"
  | "ERROR";
export type AgentDecision = "approve" | "reject";
export type AgentCommandKind = "settings" | "rerun";

export type AgentSettingsChange = {
  auto_apply?: boolean;
  interval_seconds?: number;
  slack_enabled?: boolean;
  slack_thread_ts?: string;
};
export type AgentCommand = {
  id: number;
  kind: AgentCommandKind;
  payload: Record<string, unknown>;
  requested_by: string;
  requested_at: string;
};

export type AgentStatus = {
  agent_id: string;
  version: string;
  auto_apply: boolean;
  interval_seconds: number;
  observed_at: string;
  received_at: string;
  online: boolean;
  config: Record<string, unknown> | null;
  pending_commands: AgentCommand[];
};
export type AgentStatusList = { agents: AgentStatus[]; can_command: boolean };
export type AgentChange = { key: string; value: string };
export type AgentRunSummary = {
  reason_codes: string[];
  changes: AgentChange[];
  diagnosis: string | null;
};
export type AgentRun = {
  run_id: string;
  agent_id: string;
  job_id: string;
  state: AgentRunState;
  created_at: string;
  updated_at: string;
  summary: AgentRunSummary;
};
export type AgentRunEvent = { at: string; state: string; message: string };
export type AgentPendingApproval = {
  id: number;
  decision: AgentDecision;
  requested_by: string;
  requested_at: string;
};
export type AgentRunDetail = AgentRun & {
  detail: Record<string, unknown>;
  events: AgentRunEvent[];
  pending_approval: AgentPendingApproval | null;
  can_decide: boolean;
  can_rerun: boolean;
};

export function getAgentStatus(signal?: AbortSignal) {
  return request<AgentStatusList>("agent/status", { signal });
}

export function listAgentRuns(limit: number, signal?: AbortSignal) {
  return request<{ runs: AgentRun[] }>(`agent/runs?limit=${limit}`, {
    signal,
  });
}

export function getAgentRun(runId: string, signal?: AbortSignal) {
  return request<AgentRunDetail>(`agent/runs/${encodeURIComponent(runId)}`, {
    signal,
  });
}

export function decideAgentRun(runId: string, decision: AgentDecision) {
  return request<{ id: number; decision: AgentDecision }>(
    `agent/runs/${encodeURIComponent(runId)}/${decision}`,
    { method: "POST" },
  );
}

function agentPath(agentId: string, action: string) {
  return `agent/agents/${encodeURIComponent(agentId)}/${action}`;
}

export function changeAgentSettings(
  agentId: string,
  change: AgentSettingsChange,
) {
  return request<{ id: number; kind: AgentCommandKind }>(
    agentPath(agentId, "settings"),
    { method: "POST", body: change },
  );
}

export function rerunAgent(agentId: string) {
  return request<{ id: number; kind: AgentCommandKind }>(
    agentPath(agentId, "rerun"),
    { method: "POST" },
  );
}
