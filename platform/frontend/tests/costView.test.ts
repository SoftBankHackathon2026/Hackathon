import assert from "node:assert/strict";
import test from "node:test";
import type {
  CostOverview,
  EnvironmentCost,
  MigrationCost,
} from "../src/api/costs.ts";
import {
  categoryMatrix,
  compareRows,
  envTotals,
  pickJob,
  sortedChanges,
  topResources,
} from "../src/features/costs/costView.ts";

const line = (item: string, category: string, monthly: number | null) =>
  ({
    item,
    category,
    monthly,
    quantity: 1,
    unit: "개",
    unit_price: monthly,
    basis: "list_price",
    note: null,
  }) as const;

const env = (
  id: string,
  provider: string,
  lines: ReturnType<typeof line>[],
  unpriced = 0,
  equivalent?: number,
) =>
  ({
    env_id: id,
    name: id,
    provider,
    currency: "USD",
    observed_at: null,
    current: {
      label: "",
      monthly: lines.reduce((n, l) => n + (l.monthly ?? 0), 0),
      lines,
      unpriced,
    },
    cloud_equivalent:
      equivalent === undefined
        ? null
        : {
            label: "",
            monthly: equivalent,
            lines: [line("eq", "compute", equivalent)],
            unpriced: 0,
          },
    prices: null,
    sources: [],
    gaps: [],
  }) as unknown as EnvironmentCost;

const overview = {
  generated_at: "",
  environments: [
    env("onprem", "onprem", [line("장비", "onprem", null)], 1, 145),
    env("aws", "aws", [
      line("EC2", "compute", 100),
      line("RDS", "database", 20),
      line("S3", "storage", 1),
    ]),
  ],
  migrations: [],
} as unknown as CostOverview;

test("onprem without owner input falls back to the cloud equivalent", () => {
  const t = envTotals(overview);
  assert.equal(t.rows[0].estimated, true);
  assert.equal(t.rows[0].monthly, 145);
  assert.equal(t.cloud, 121);
  assert.equal(t.total, 266);
});

test("category matrix orders categories by total across environments", () => {
  const m = categoryMatrix(overview);
  assert.deepEqual(m.categories, ["compute", "database", "storage"]);
  assert.equal(m.totals.get("compute"), 245);
});

test("top resources are the most expensive lines anywhere", () => {
  assert.deepEqual(
    topResources(overview, 2).map((r) => r.line.item),
    ["eq", "EC2"],
  );
});

test("compareRows builds the before, change, after table with totals", () => {
  const m = {
    source_env: "onprem",
    target_env: "aws",
    source_monthly: 145,
    source_released: 0,
    target_monthly_now: 400,
    target_monthly_after: 550,
    monthly_change: 150,
  } as unknown as MigrationCost;
  const { rows, total } = compareRows(m);
  assert.deepEqual(
    rows.map((r) => [r.role, r.now, r.change, r.after]),
    [
      ["원본", 145, -0, 145],
      ["대상", 400, 150, 550],
    ],
  );
  assert.deepEqual(total, { now: 545, change: 150, after: 695 });
});

test("compareRows uses the estimate for an on-prem target that stays the same", () => {
  const m = {
    source_env: "aws",
    target_env: "onprem",
    source_monthly: 400,
    source_released: 28,
    target_monthly_now: 0,
    target_monthly_after: 0,
    monthly_change: -28,
  } as unknown as MigrationCost;
  const { rows, total } = compareRows(m, (id) =>
    id === "onprem" ? 145 : null,
  );
  assert.deepEqual(
    rows.map((r) => [r.role, r.now, r.change, r.after]),
    [
      ["원본", 400, -28, 372],
      ["대상", 145, 0, 145],
    ],
  );
  assert.deepEqual(total, { now: 545, change: -28, after: 517 });
});

test("changes are sorted by monthly change with unpriced rows last", () => {
  const rows = sortedChanges([
    { delta: 1 },
    { delta: null },
    { delta: 80 },
  ] as never);
  assert.deepEqual(
    rows.map((r: { delta: number | null }) => r.delta),
    [80, 1, null],
  );
});

test("pickJob keeps a listed job and falls back to the first one", () => {
  const jobs = [{ job_id: "a" }, { job_id: "b" }] as MigrationCost[];
  assert.equal(pickJob(jobs, "b"), "b");
  assert.equal(pickJob(jobs, "gone"), "a");
  assert.equal(pickJob(jobs, ""), "a");
  assert.equal(pickJob([], "a"), "");
});
