import { keepPreviousData, queryOptions } from "@tanstack/react-query";
import {
  getEvents,
  getFleet,
  getMigration,
  getSeries,
  type Fleet,
} from "../../api/migrations";
import { isActive, pollInterval } from "./stages";

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
