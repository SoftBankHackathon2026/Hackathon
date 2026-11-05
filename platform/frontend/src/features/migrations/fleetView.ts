import type {
  FleetEnvironment,
  MigrationSummary,
} from "../../api/migrations.ts";

export const PROVIDER_LABELS: Record<FleetEnvironment["provider"], string> = {
  onprem: "온프렘",
  aws: "AWS",
  gcp: "GCP",
};

export const KIND_LABELS: Record<FleetEnvironment["kind"], string> = {
  k3s: "k3s",
  eks: "EKS",
  gke: "GKE",
};

export const DIRECTION_LABELS: Record<MigrationSummary["direction"], string> = {
  forward: "이전",
  return: "복귀",
};

export function envShortName(environments: FleetEnvironment[], id: string) {
  const env = environments.find((e) => e.id === id);
  return env ? `${PROVIDER_LABELS[env.provider]} ${KIND_LABELS[env.kind]}` : id;
}

export function envName(environments: FleetEnvironment[], id: string) {
  return environments.find((e) => e.id === id)?.name ?? id;
}

export function appRows(environments: FleetEnvironment[]) {
  const names: string[] = [];
  for (const env of environments)
    for (const app of env.apps)
      if (!names.includes(app.name)) names.push(app.name);
  return names;
}
