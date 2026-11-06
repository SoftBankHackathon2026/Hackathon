export type Provider = "kubernetes" | "aws" | "gcp";
export type Scope =
  "inventory.read" | "data.read" | "secrets.read" | "decrypt.use";
export type ConnectionTarget = {
  organization: string;
  project: string;
  name: string;
  kind: Provider;
  role: "source" | "target";
  region: string;
  expectedIdentity: string;
  scopes: Scope[];
};
export type ConnectionPlan = {
  id: string;
  digest: string;
  targetIdentity: string;
  expiresAt: number;
  title: string;
  version: string;
  definition: string;
  privileges: string[];
  changes: string[];
  restart: string;
  recovery: string;
  artifacts: { name: string; digest: string }[];
};
export type ConnectionSnapshot = {
  id: string;
  revision: number;
  target: ConnectionTarget;
  serverNow: number;
  observedAt: number;
  registrationExpiresAt: number;
  approvalExpiresAt?: number;
  registration: "ISSUED" | "CONSUMED" | "EXPIRED" | "CANCELLED";
  device: "NONE" | "PENDING_APPROVAL" | "APPROVED" | "REJECTED" | "REVOKED";
  tunnel: "NONE" | "CONNECTING" | "READY" | "REKEYING" | "DEGRADED";
  planState: "NONE" | "PENDING" | "APPROVED";
  job: "NONE" | "RUNNING" | "SUCCEEDED" | "PARTIAL_FAILURE" | "UNKNOWN_RESULT";
  permissions: {
    approveDevice: boolean;
    approvePlan: boolean;
    revoke: boolean;
  };
  candidate?: {
    id: string;
    digest: string;
    fingerprint: string;
    hostname: string;
    os: string;
    arch: string;
  };
  attemptsLeft: number;
  tunnelChecks: number;
  targetIdentity?: string;
  contexts: string[];
  selectedContext?: string;
  plan?: ConnectionPlan;
  jobs: {
    name: string;
    state: "waiting" | "running" | "done" | "failed" | "unknown";
    detail: string;
  }[];
  environmentId?: string;
  verifiedAt?: number;
  epoch: number;
  keyExpiresAt?: number;
  cleanup?: {
    server: boolean;
    gateway: boolean;
    resources: boolean;
    local: boolean;
  };
  blockedUntil?: number;
  issue?: string;
  events: { at: number; message: string }[];
};

// Browser-facing proposal; missing read/cancel endpoints are never assumed.
export type ConnectionGateway = {
  mode: "unavailable" | "preview";
  available: boolean;
  reason?: string;
  list: () => readonly ConnectionSnapshot[];
  subscribe: (listener: () => void) => () => void;
  create: (
    target: ConnectionTarget,
    publicKey: string,
    operationId: string,
  ) => Promise<ConnectionSnapshot>;
  cancel: (id: string) => Promise<void>;
  approveDevice: (
    id: string,
    candidateDigest: string,
    code: string,
  ) => Promise<void>;
  reject: (id: string) => Promise<void>;
  inspect: (id: string, context?: string) => Promise<void>;
  approvePlan: (id: string, digest: string) => Promise<void>;
  reconnect: (id: string) => Promise<void>;
  revoke: (id: string) => Promise<void>;
};
