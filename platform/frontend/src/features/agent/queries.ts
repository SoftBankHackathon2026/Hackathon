import {
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import {
  decideAgentRun,
  getAgentRun,
  getAgentStatus,
  listAgentRuns,
  type AgentDecision,
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
