import type { Workload } from "../../src/features/prepare/workloadGroups";

// Representative dataset reconstructed from the brief. Its 22 named entries
// are retained; system workload identities are illustrative.
const workload = (
  name: string,
  images: string[],
  overrides: Partial<Workload> = {},
): Workload => ({
  namespace: "hackathon",
  kind: "Deployment",
  name,
  replicas: 1,
  images,
  secrets: [],
  configMaps: [],
  pvcs: [],
  ...overrides,
});

const systemCounts = {
  "kube-system": 4,
  argocd: 6,
  harbor: 7,
  monitoring: 4,
  "cert-manager": 3,
  "argo-rollouts": 1,
  "edge-system": 1,
  // The brief gives a 29-item system total but only accounts for 26 by namespace.
  "ingress-nginx": 1,
  tailscale: 1,
  "local-path-storage": 1,
};

const migrationImage = (name: string) =>
  `ghcr.io/softbankhackathon2026/${name}:v1`;

export const appWorkloads: Workload[] = [
  workload("sample-app", ["kty1021/sample-app:6ce6e5f"], {
    kind: "Rollout",
    replicas: 2,
  }),
  workload("review-api", ["registry/review-api:v1", "postgres:16.15-alpine"], {
    secrets: ["review-db"],
    configMaps: ["review-config"],
  }),
  workload("review-worker", ["registry/review-worker:v1"]),
  workload("sample-app-rehearsal", ["kty1021/sample-app:6ce6e5f"]),
  workload("ai-agent", ["registry/ai-agent:v1"], { replicas: 0 }),
  workload("sample-app-rds", ["kty1021/sample-app:6ce6e5f"]),
  workload("postgres", ["postgres:16.15-alpine"]),
  workload("kafka-broker", ["apache/kafka:3.9.1"]),
  workload("kafka-controller", ["apache/kafka:3.9.1"]),
  workload("durable-ingress-redis", ["redis:7"]),
  workload("qdrant", ["qdrant/qdrant:v1"]),
  workload("object-storage", ["chrislusf/seaweedfs:4.52"]),
  workload("openbao", ["quay.io/openbao/openbao:2.7.2"]),
  workload("db-insert-worker", ["registry/worker:v1"]),
  workload("durable-ingress", ["registry/ingress:v1"]),
  workload("live-db-cutover", ["registry/cutover:v1"]),
  workload("migration-connect", [migrationImage("migration-connect")]),
  workload("migration-policy", [migrationImage("migration-policy")]),
  workload("policy-roundtrip", ["registry/roundtrip:v1"]),
  workload("platform-status-proxy", ["registry/proxy:v1"]),
  workload("traffic-bot", ["kty1021/sample-app:6ce6e5f"]),
  workload("sample-app-loadgen", ["kty1021/sample-app:6ce6e5f"]),
  ...Object.entries(systemCounts).flatMap(([namespace, count]) =>
    Array.from({ length: count }, (_, i) =>
      workload(
        `${namespace}-component-${i + 1}`,
        [`registry/${namespace}:v1`],
        { namespace },
      ),
    ),
  ),
];
