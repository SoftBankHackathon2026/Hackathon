import { queryOptions } from "@tanstack/react-query";
import { getCosts } from "../../api/costs";

// 자원 목록은 수집기가 약 5분마다 갱신한다.
const COSTS_POLL_MS = 60000;

export function costsQueryOptions() {
  return queryOptions({
    queryKey: ["costs"] as const,
    queryFn: ({ signal }) => getCosts(signal),
    refetchInterval: COSTS_POLL_MS,
  });
}
