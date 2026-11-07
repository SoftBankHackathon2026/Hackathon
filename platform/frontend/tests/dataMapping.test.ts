import assert from "node:assert/strict";
import test from "node:test";
import type { DataMapping, DataResource } from "../src/api/dataMapping.ts";
import {
  basisText,
  changed,
  choiceValue,
  choose,
  draftFrom,
  draftProblem,
  resourceLabel,
  setReason,
  statusTone,
  updateBody,
} from "../src/features/migrations/dataMapping.ts";

const resource = (
  id: string,
  name: string,
  extra: Partial<DataResource> = {},
) =>
  ({
    id,
    env_id: "onprem",
    kind: "postgres",
    name,
    origin: "cluster",
    detail: null,
    infrastructure: false,
    ...extra,
  }) as DataResource;

const mapping = (overrides: Partial<DataMapping> = {}): DataMapping => ({
  job_id: "forward_001",
  source_env: "onprem",
  target_env: "aws",
  inventory_hash: "h1",
  status: "needs_review",
  required: true,
  confirmed_by: null,
  confirmed_at: null,
  sources: [
    resource("s1", "hackathon/postgres"),
    resource("s2", "orders/db-1"),
  ],
  targets: [
    resource("t1", "rds-a", { origin: "managed", detail: "db.t4g.micro" }),
  ],
  bindings: [
    {
      source: "s1",
      action: "map",
      target: "t1",
      reason: null,
      kind: "postgres",
      basis: "name",
      candidates: ["t1"],
      note: null,
    },
    {
      source: "s2",
      action: "map",
      target: null,
      reason: null,
      kind: "postgres",
      basis: "none",
      candidates: ["t1"],
      note: null,
    },
  ],
  blockers: [],
  ...overrides,
});

test("choices switch between target, create and exclude", () => {
  const draft = draftFrom(mapping());
  assert.equal(choiceValue(draft.s1), "t1");
  assert.equal(choiceValue(draft.s2), "");
  const created = choose(draft, "s1", "create");
  assert.deepEqual(created.s1, {
    source: "s1",
    action: "create",
    target: null,
    reason: null,
  });
  assert.equal(choiceValue(created.s1), "create");
  const excluded = choose(draft, "s2", "exclude");
  assert.equal(excluded.s2.action, "exclude");
  assert.equal(choose(excluded, "s2", "t1").s2.target, "t1");
  assert.equal(choose(excluded, "s2", "t1").s2.action, "map");
  assert.equal(choose(draft, "s1", "").s1.target, null);
  assert.equal(draft.s1.target, "t1");
});

test("problems name the first unfinished source", () => {
  const m = mapping();
  const draft = draftFrom(m);
  assert.equal(
    draftProblem(draft, m.sources),
    "orders/db-1 를 옮길 대상을 골라야 해요.",
  );
  const excluded = choose(choose(draft, "s2", "exclude"), "s1", "create");
  assert.equal(
    draftProblem(excluded, m.sources),
    "orders/db-1 를 옮기지 않는 이유를 적어야 해요.",
  );
  assert.equal(
    draftProblem(setReason(excluded, "s2", "  "), m.sources),
    "orders/db-1 를 옮기지 않는 이유를 적어야 해요.",
  );
  assert.equal(
    draftProblem(setReason(excluded, "s2", "안 씀"), m.sources),
    null,
  );
  assert.equal(
    draftProblem({}, m.sources),
    "hackathon/postgres 를 정해야 해요.",
  );
});

test("update body follows source order and drops fields the action does not use", () => {
  const m = mapping();
  const draft = setReason(
    choose(choose(draftFrom(m), "s2", "exclude"), "s1", "t1"),
    "s2",
    "  안 씀 ",
  );
  draft.s1 = { ...draft.s1, reason: "stray" };
  assert.deepEqual(updateBody(m, draft), {
    inventory_hash: "h1",
    bindings: [
      { source: "s1", action: "map", target: "t1", reason: null },
      { source: "s2", action: "exclude", target: null, reason: "안 씀" },
    ],
  });
  const create = choose(draftFrom(m), "s2", "create");
  create.s2 = { ...create.s2, target: "t1" };
  assert.equal(updateBody(m, create).bindings[1].target, null);
});

test("changed is always true until ready, then compares with saved values", () => {
  const pending = mapping();
  assert.equal(changed(pending, draftFrom(pending)), true);
  const ready = mapping({ status: "ready" });
  const draft = draftFrom(ready);
  assert.equal(changed(ready, draft), false);
  assert.equal(changed(ready, choose(draft, "s2", "t1")), true);
  assert.equal(changed(ready, setReason(draft, "s1", "x")), true);
  assert.equal(changed(ready, choose(draft, "s1", "create")), true);
  const { s2: _, ...missing } = draft;
  assert.equal(changed(ready, missing), true);
});

test("labels and tones", () => {
  assert.equal(resourceLabel(resource("a", "pg")), "pg (클러스터)");
  assert.equal(
    resourceLabel(
      resource("a", "logs", { origin: "managed", infrastructure: true }),
    ),
    "logs (관리형, 인프라)",
  );
  assert.equal(basisText("only_pair", null), "자동: 같은 종류가 하나씩");
  assert.equal(
    basisText("only_pair", "인프라 버킷 2개 제외"),
    "자동: 같은 종류가 하나씩 (인프라 버킷 2개 제외)",
  );
  assert.equal(
    resourceLabel(
      resource("a", "rds", { origin: "managed", detail: "db.t4g.micro" }),
    ),
    "rds (관리형, db.t4g.micro)",
  );
  assert.equal(statusTone("ready"), "ok");
  assert.equal(statusTone("unavailable"), "idle");
  assert.equal(statusTone("needs_review"), "warn");
  assert.equal(statusTone("stale"), "warn");
});
