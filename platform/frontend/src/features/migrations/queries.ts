import {
  keepPreviousData,
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import {
  getEvents,
  getFleet,
  getMigration,
  getSeries,
  listCommands,
  requestCommand,
  type CommandRequest,
  type Fleet,
} from "../../api/migrations";
import {
  confirmDataMapping,
  getDataMapping,
  type DataMappingUpdate,
} from "../../api/dataMapping";
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
