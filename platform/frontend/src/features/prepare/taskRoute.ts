import type { MigrationCreate, Provider } from "../../api/taskPlan";

export type TaskEnvironment = { id: string; name: string; provider: Provider };

export function taskRequest(
  source: TaskEnvironment,
  target: TaskEnvironment,
  apps: string[],
): MigrationCreate {
  return {
    source_env: source.id,
    source_provider: source.provider,
    target_env: target.id,
    target_provider: target.provider,
    apps,
  };
}
