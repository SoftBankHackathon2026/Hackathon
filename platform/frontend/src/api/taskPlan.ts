import { request } from "./client";

export type StepStatus =
  "pending" | "running" | "waiting" | "done" | "failed" | "unsupported";

export type PlanStep = {
  id: string;
  title: string;
  description: string;
  tool: string | null;
  status: StepStatus;
  runnable: boolean;
};

export type PlanPhase = {
  id: "environment" | "application";
  title: string;
  status: StepStatus;
  steps: PlanStep[];
};

export type TaskPlan = {
  job_id: string;
  route: {
    source_env: string;
    target_env: string;
    source_provider: string;
    target_provider: string;
    label: string;
  };
  phases: PlanPhase[];
};

export function getTaskPlan(jobId: string, signal?: AbortSignal) {
  return request<TaskPlan>(`migrations/${encodeURIComponent(jobId)}/plan`, {
    signal,
  });
}
