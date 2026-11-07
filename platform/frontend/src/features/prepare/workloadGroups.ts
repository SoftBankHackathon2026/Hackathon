import type { ConnectionSnapshot } from "./connectionTypes";

export type Workload = NonNullable<
  ConnectionSnapshot["workloads"]
>["items"][number];
export type WorkloadGroup = "app" | "data" | "migration" | "infrastructure";

const systemNamespaces = new Set([
  "kube-system",
  "kube-public",
  "kube-node-lease",
  "argocd",
  "argo-rollouts",
  "cert-manager",
  "monitoring",
  "harbor",
  "edge-system",
  "ingress-nginx",
  "tailscale",
  "kube-flannel",
  "local-path-storage",
]);
const migrationNames = new Set([
  "db-insert-worker",
  "durable-ingress",
  "live-db-cutover",
  "policy-roundtrip",
  "traffic-bot",
  "platform-status-proxy",
]);
const dataImages = new Set([
  "postgres",
  "mysql",
  "mariadb",
  "redis",
  "valkey",
  "kafka",
  "zookeeper",
  "rabbitmq",
  "qdrant",
  "elasticsearch",
  "opensearch",
  "minio",
  "seaweedfs",
  "openbao",
  "vault",
  "mongo",
]);

export function classifyWorkload(
  workload: Pick<Workload, "namespace" | "kind" | "name" | "images" | "pvcs">,
): WorkloadGroup {
  if (
    systemNamespaces.has(workload.namespace) ||
    workload.kind === "DaemonSet"
  ) {
    return "infrastructure";
  }
  const repositories = workload.images.map((image) =>
    image.split("@", 1)[0].replace(/:[^/]+$/, ""),
  );
  if (
    migrationNames.has(workload.name) ||
    workload.name.endsWith("-loadgen") ||
    repositories.some((repo) =>
      /^ghcr\.io\/softbankhackathon2026\/(?:migration-[^/]+|ghost-agent)$/.test(
        repo,
      ),
    )
  ) {
    return "migration";
  }
  if (
    repositories.length > 0 &&
    repositories.every((repo) => dataImages.has(repo.split("/").at(-1)!))
  ) {
    return "data";
  }
  if (workload.kind === "StatefulSet" && workload.pvcs.length > 0) {
    return "data";
  }
  return "app";
}
