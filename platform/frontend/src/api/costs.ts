import { request } from "./client";

export type CostBasis =
  "list_price" | "owner_input" | "measured" | "not_priced";
export type CostLine = {
  category: string;
  item: string;
  quantity: number;
  unit: string;
  unit_price: number | null;
  monthly: number | null;
  basis: CostBasis;
  note: string | null;
};
export type CostBreakdown = {
  label: string;
  monthly: number;
  lines: CostLine[];
  unpriced: number;
};
export type PriceRef = {
  provider: string;
  region: string;
  source: string;
  fetched_at: string;
};
export type EnvironmentCost = {
  env_id: string;
  name: string;
  provider: "onprem" | "aws" | "gcp";
  currency: "USD";
  observed_at: string | null;
  current: CostBreakdown | null;
  cloud_equivalent: CostBreakdown | null;
  prices: PriceRef | null;
  sources: string[];
  gaps: string[];
};
export type CapacityCheck = {
  need_vcpu: number;
  need_memory_gib: number;
  free_vcpu: number;
  free_memory_gib: number;
};
export type MigrationCost = {
  job_id: string;
  source_env: string;
  target_env: string;
  currency: "USD";
  source_monthly: number | null;
  target_monthly_now: number | null;
  target_monthly_after: number | null;
  added: CostBreakdown | null;
  source_released: number | null;
  monthly_change: number | null;
  capacity: CapacityCheck | null;
  capacity_fits: boolean | null;
  dual_run_days: number;
  dual_run_cost: number | null;
  assumptions: string[];
  gaps: string[];
};
export type CostOverview = {
  generated_at: string;
  environments: EnvironmentCost[];
  migrations: MigrationCost[];
};

export function getCosts(signal?: AbortSignal) {
  return request<CostOverview>("costs", { signal });
}
