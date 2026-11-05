import { queryOptions } from "@tanstack/react-query";
import { getPosture } from "../../api/security";

const POSTURE_POLL_MS = 60000;

export function postureQueryOptions() {
  return queryOptions({
    queryKey: ["security", "posture"] as const,
    queryFn: ({ signal }) => getPosture(signal),
    refetchInterval: POSTURE_POLL_MS,
  });
}
