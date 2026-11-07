export type Provider = "kubernetes" | "aws" | "gcp";
export type CollectionContext = { organization: string };
export type ConnectionTarget = CollectionContext & {
  project: string;
  name: string;
  kind: Provider;
  role: "source" | "target";
  region: string;
};
export type DiscoveredEnvironment = {
  id: string;
  kind: Provider;
  name: string;
  identity: string;
  region: string;
  context?: string;
};
export type CollectedEnvironment = DiscoveredEnvironment & {
  facts: { label: string; value: string }[];
};
export type ConnectionSnapshot = {
  id: string;
  revision: number;
  organization: string;
  target: ConnectionTarget | null;
  serverNow: number;
  observedAt: number;
  lastTunnelActivityAt?: number;
  registrationExpiresAt: number;
  approvalExpiresAt?: number;
  registration: "ISSUED" | "CONSUMED" | "EXPIRED" | "CANCELLED";
  device: "NONE" | "PENDING_APPROVAL" | "APPROVED" | "REJECTED" | "REVOKED";
  tunnel: "NONE" | "CONNECTING" | "READY" | "REKEYING" | "DEGRADED";
  collection: "WAITING" | "COLLECTED";
  collectionPending?: boolean;
  discovered: DiscoveredEnvironment[];
  collected: CollectedEnvironment[];
  workloads?: {
    coverage: "complete" | "unavailable";
    rolloutsAvailable?: boolean;
    items: {
      namespace: string;
      kind: string;
      name: string;
      replicas: number;
      images: string[];
      secrets: string[];
      configMaps: string[];
      pvcs: string[];
    }[];
  } | null;
  collectedAt?: number;
  inventoryDigest?: string;
  selectedEnvironmentId?: string;
  environmentId?: string;
  registeredAt?: number;
  permissions: {
    approveDevice: boolean;
    registerEnvironment: boolean;
    revoke: boolean;
  };
  candidate?: {
    id: string;
    digest: string;
    fingerprint: string;
    hostname: string;
    os: string;
    arch: string;
    supportsReplacement?: boolean;
  };
  attemptsLeft: number;
  tunnelChecks: number;
  replacement?: {
    previousRequestId: string;
    gateway: "pending" | "blocked" | "ready";
    client: "pending" | "blocked" | "ready";
  };
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
  deployment?: { bootstrapUrl: string; releaseVersion: string };
};

// Preview and live implementations share this view contract. The server remains
// authoritative for permissions, expiry, tunnel readiness and registration.
export type ConnectionGateway = {
  mode: "unavailable" | "preview" | "live";
  available: boolean;
  reason?: string;
  list: () => readonly ConnectionSnapshot[];
  subscribe: (listener: () => void) => () => void;
  create: (
    context: CollectionContext,
    publicKey: string,
    operationId: string,
  ) => Promise<ConnectionSnapshot>;
  cancel: (id: string) => Promise<void>;
  approveDevice: (
    id: string,
    candidateDigest: string,
    code: string,
    replaceId?: string,
  ) => Promise<void>;
  reject: (id: string) => Promise<void>;
  collect: (id: string, discoveredId: string) => Promise<void>;
  register: (
    id: string,
    collectedId: string,
    inventoryDigest: string,
    target: ConnectionTarget,
  ) => Promise<void>;
  reconnect: (id: string) => Promise<void>;
  revoke: (id: string) => Promise<void>;
};
