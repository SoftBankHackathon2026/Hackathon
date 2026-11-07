import type { MigrationCreate, Provider } from "../../api/taskPlan";

export const PROVIDER_OPTIONS: { value: Provider; label: string }[] = [
  { value: "onprem", label: "온프렘" },
  { value: "aws", label: "AWS" },
  { value: "gcp", label: "GCP" },
];

export function taskRequest(
  connected: { id: string; role: "source" | "target"; provider: Provider },
  counterpart: string,
  apps: string[],
): MigrationCreate {
  if (connected.role === "source")
    return {
      source_env: connected.id,
      source_provider: connected.provider,
      target_env: counterpart,
      apps,
    };
  return {
    source_env: counterpart,
    target_env: connected.id,
    target_provider: connected.provider,
    apps,
  };
}
