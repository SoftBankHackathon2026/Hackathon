import { useState } from "react";
import type { Posture } from "../../api/security";
import {
  SHOWN_STATUSES,
  defaultFilters,
  type ExplorerFilters,
} from "./explorer";

export function useFindingFilters(posture: Posture, focus: string | undefined) {
  const envIds = posture.environments.map((e) => e.env_id);
  const standardIds = posture.standards.map((s) => s.id);
  const [filters, setFilters] = useState<ExplorerFilters>(() => {
    const base = defaultFilters(envIds, standardIds);
    const target = posture.controls.find((c) => c.id === focus);
    if (!target) return base;
    return {
      ...base,
      status: SHOWN_STATUSES.filter((s) =>
        Object.values(target.results).some((r) => r.status === s),
      ),
    };
  });
  return {
    filters,
    set: (patch: Partial<ExplorerFilters>) =>
      setFilters((prev) => ({ ...prev, ...patch })),
    reset: () => setFilters(defaultFilters(envIds, standardIds)),
  };
}
