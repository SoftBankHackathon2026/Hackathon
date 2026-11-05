import type { EnvComponent, FleetEnvironment } from "../../api/migrations.ts";

export type CloudProvider = "aws" | "gcp";
export type CloudIconRef = { provider: CloudProvider; file: string };

const AWS_SERVICES: Record<string, string> = {
  postgres: "rds",
  kafka: "msk",
  object_storage: "s3",
  load_balancer: "elb",
  monitoring: "cloudwatch",
};

const GCP_SERVICES: Record<string, string> = {
  postgres: "cloud-sql",
  cache: "memorystore",
  object_storage: "cloud-storage",
  load_balancer: "load-balancing",
  monitoring: "logging",
  serverless: "cloud-run",
};

const AWS_COLORS: Record<string, string> = {
  rds: "#c925d1",
  msk: "#8c4fff",
  s3: "#7aa116",
  elb: "#8c4fff",
  cloudwatch: "#e7157b",
  eks: "#ed7100",
};

export function serviceColor(ref: CloudIconRef | null) {
  return ref && ref.provider === "aws" ? (AWS_COLORS[ref.file] ?? null) : null;
}

export function cloudProviderOf(
  env: Pick<FleetEnvironment, "provider">,
): CloudProvider | null {
  return env.provider === "aws" || env.provider === "gcp" ? env.provider : null;
}

export function serviceIcon(
  provider: CloudProvider | null,
  component: Pick<EnvComponent, "kind" | "managed">,
): CloudIconRef | null {
  if (!provider || !component.managed) return null;
  const table = provider === "aws" ? AWS_SERVICES : GCP_SERVICES;
  const file = table[component.kind];
  return file ? { provider, file } : null;
}

export function groupIcon(
  provider: CloudProvider | null,
  group: { kind: string; label: string },
): CloudIconRef | null {
  if (provider === "aws") {
    if (group.label.startsWith("AWS 계정"))
      return { provider, file: "group-cloud" };
    if (group.label.startsWith("리전"))
      return { provider, file: "group-region" };
    if (group.label.startsWith("VPC")) return { provider, file: "group-vpc" };
    if (group.kind === "subnet")
      return {
        provider,
        file: group.label.includes("public")
          ? "group-public-subnet"
          : "group-private-subnet",
      };
    if (group.kind === "cluster" && group.label.startsWith("EKS"))
      return { provider, file: "eks" };
    return null;
  }
  if (
    provider === "gcp" &&
    group.kind === "cluster" &&
    group.label.startsWith("GKE")
  )
    return { provider, file: "gke" };
  return null;
}

export function iconKey(ref: CloudIconRef) {
  return `${ref.provider}/${ref.file}`;
}
