import {
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import {
  changeAgentSettings,
  decideAgentRun,
  deleteAgentRuns,
  getAgentRun,
  getAgentStatus,
  listAgentRuns,
  rerunAgent,
  type AgentDecision,
  type AgentRunDeletion,
  type AgentSettingsChange,
} from "../../api/agent";

export const AGENT_POLL_MS = 5000;
export const RUN_LIMIT = 100;

export function agentStatusQueryOptions() {
  return queryOptions({
    queryKey: ["agent", "status"] as const,
    queryFn: ({ signal }) => getAgentStatus(signal),
    refetchInterval: AGENT_POLL_MS,
  });
}

export function agentRunsQueryOptions() {
  return queryOptions({
    queryKey: ["agent", "runs"] as const,
    queryFn: ({ signal }) => listAgentRuns(RUN_LIMIT, signal),
    refetchInterval: AGENT_POLL_MS,
  });
}

export function agentRunQueryOptions(runId: string) {
  return queryOptions({
    queryKey: ["agent", "run", runId] as const,
    queryFn: ({ signal }) => getAgentRun(runId, signal),
    refetchInterval: AGENT_POLL_MS,
  });
}

export function useDecideAgentRun(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (decision: AgentDecision) => decideAgentRun(runId, decision),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["agent"] }),
  });
}

export function useChangeAgentSettings(agentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (change: AgentSettingsChange) =>
      changeAgentSettings(agentId, change),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["agent"] }),
  });
}

export function useRerunAgent(agentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => rerunAgent(agentId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["agent"] }),
  });
}

/** 결과 처리는 훅 단계에서 한다. 지운 실행의 패널이 먼저 닫혀도 결과 문장을 놓치지 않게. */
export function useDeleteAgentRuns(onDone: (result: AgentRunDeletion) => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (runIds: string[]) => deleteAgentRuns(runIds),
    onSuccess: onDone,
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["agent"] });
    },
  });
}
