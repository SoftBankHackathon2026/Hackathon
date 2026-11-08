import {
  keepPreviousData,
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import {
  deleteMigrations,
  getEvents,
  getFleet,
  getMigration,
  getSeries,
  listCommands,
  requestCommand,
  type CommandRequest,
  type Fleet,
  type TaskDeletion,
} from "../../api/migrations";
import {
  confirmDataMapping,
  getDataMapping,
  type DataMappingUpdate,
} from "../../api/dataMapping";
import { withoutDeleted } from "./deletion";
import { isActive, pollInterval } from "./stages";
import { getTaskPlan } from "../../api/taskPlan";

export const SERIES_MINUTES = 60;

export function fleetPollInterval(fleet: Fleet | undefined) {
  return fleet?.migrations.some((m) => isActive(m.status)) ? 5000 : 30000;
}

export function fleetQueryOptions() {
  return queryOptions({
    queryKey: ["fleet"] as const,
    queryFn: ({ signal }) => getFleet(signal),
    refetchInterval: (query) => fleetPollInterval(query.state.data),
  });
}

export function migrationQueryOptions(jobId: string) {
  return queryOptions({
    queryKey: ["migration", jobId] as const,
    queryFn: ({ signal }) => getMigration(jobId, signal),
    refetchInterval: (query) => pollInterval(query.state.data?.summary.status),
  });
}

export function seriesQueryOptions(
  jobId: string,
  interval: number,
  minutes = SERIES_MINUTES,
) {
  return queryOptions({
    queryKey: ["migration", jobId, "series", minutes] as const,
    queryFn: ({ signal }) => getSeries(jobId, minutes, signal),
    refetchInterval: interval,
    placeholderData: keepPreviousData,
  });
}

export function eventsQueryOptions(jobId: string, interval: number) {
  return queryOptions({
    queryKey: ["migration", jobId, "events"] as const,
    queryFn: ({ signal }) => getEvents(jobId, signal),
    refetchInterval: interval,
  });
}

export function commandsQueryOptions(jobId: string, interval: number) {
  return queryOptions({
    queryKey: ["migration", jobId, "commands"] as const,
    queryFn: ({ signal }) => listCommands(jobId, signal),
    refetchInterval: interval,
  });
}

/** 명령을 보낸 뒤 이 작업의 상세·명령·이벤트 질의를 모두 다시 읽는다. */
export function useRequestCommand(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CommandRequest) => requestCommand(jobId, body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["migration", jobId] }),
  });
}

export function taskPlanQueryOptions(jobId: string) {
  return queryOptions({
    queryKey: ["migration", jobId, "plan"] as const,
    queryFn: ({ signal }) => getTaskPlan(jobId, signal),
    refetchInterval: 10000,
  });
}

export function dataMappingQueryOptions(jobId: string) {
  return queryOptions({
    queryKey: ["migration", jobId, "data-mapping"] as const,
    queryFn: ({ signal }) => getDataMapping(jobId, signal),
    refetchInterval: 60000,
  });
}

export function useConfirmDataMapping(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: DataMappingUpdate) => confirmDataMapping(jobId, body),
    onSuccess: (mapping) => {
      queryClient.setQueryData(["migration", jobId, "data-mapping"], mapping);
      return queryClient.invalidateQueries({
        queryKey: ["migration", jobId, "commands"],
      });
    },
  });
}

/**
 * 작업 지우기. 결과 처리는 훅 단계에서 한다. 상세 화면이 먼저 닫혀도 결과 문장을 놓치지 않게.
 * 결과를 먼저 넘기고(상세 화면은 이때 개요로 돌아감), 지운 작업을 개요 목록에서 빼고 상세 질의를 버린 뒤
 * 개요와 비용을 다시 읽는다.
 */
export function useDeleteMigrations(onDone: (result: TaskDeletion) => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobIds: string[]) => deleteMigrations(jobIds),
    onSuccess: (result) => {
      onDone(result);
      queryClient.setQueryData<Fleet>(["fleet"], (fleet) =>
        fleet ? withoutDeleted(fleet, result.deleted) : fleet,
      );
      for (const jobId of result.deleted)
        queryClient.removeQueries({ queryKey: ["migration", jobId] });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["fleet"] });
      void queryClient.invalidateQueries({ queryKey: ["costs"] });
    },
  });
}
