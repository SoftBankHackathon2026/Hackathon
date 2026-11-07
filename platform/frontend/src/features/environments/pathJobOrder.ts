import type { MigrationStatus, MigrationSummary } from "../../api/migrations";

const ORDER: Record<MigrationStatus, number> = {
  running: 0,
  waiting_approval: 0,
  needs_recovery: 1,
  aborted: 1,
  pending: 2,
  rolled_back: 3,
  completed: 3,
};

export function isActive(status: MigrationStatus): boolean {
  return ORDER[status] === 0;
}

export function orderedJobs(
  migrations: MigrationSummary[],
): MigrationSummary[] {
  return [...migrations].sort(
    (a, b) =>
      ORDER[a.status] - ORDER[b.status] ||
      (b.updated_at ?? "").localeCompare(a.updated_at ?? "") ||
      a.job_id.localeCompare(b.job_id),
  );
}

export function pickJob(
  migrations: MigrationSummary[],
  chosen: string | null,
): MigrationSummary | null {
  return (
    migrations.find((m) => m.job_id === chosen) ??
    orderedJobs(migrations)[0] ??
    null
  );
}
