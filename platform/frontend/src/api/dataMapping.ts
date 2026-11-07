import { request } from "./client";

export type DataKind = "postgres" | "kafka" | "object_storage";
export type BindingAction = "map" | "create" | "exclude";
export type BindingBasis =
  "only_pair" | "no_target" | "name" | "none" | "confirmed";
export type MappingStatus = "ready" | "needs_review" | "stale" | "unavailable";

export type DataResource = {
  id: string;
  env_id: string;
  kind: DataKind;
  name: string;
  origin: "managed" | "cluster";
  detail: string | null;
  infrastructure: boolean;
};

export type DataBinding = {
  source: string;
  action: BindingAction;
  target: string | null;
  reason: string | null;
};

export type BindingView = DataBinding & {
  kind: DataKind;
  basis: BindingBasis;
  candidates: string[];
  note: string | null;
};

export type DataMapping = {
  job_id: string;
  source_env: string;
  target_env: string;
  inventory_hash: string;
  status: MappingStatus;
  required: boolean;
  confirmed_by: string | null;
  confirmed_at: string | null;
  sources: DataResource[];
  targets: DataResource[];
  bindings: BindingView[];
  blockers: string[];
};

export type DataMappingUpdate = {
  inventory_hash: string;
  bindings: DataBinding[];
};

function path(jobId: string) {
  return `migrations/${encodeURIComponent(jobId)}/data-mapping`;
}

export function getDataMapping(jobId: string, signal?: AbortSignal) {
  return request<DataMapping>(path(jobId), { signal });
}

export function confirmDataMapping(jobId: string, body: DataMappingUpdate) {
  return request<DataMapping>(path(jobId), { method: "POST", body });
}
