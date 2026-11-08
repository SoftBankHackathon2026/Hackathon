import assert from "node:assert/strict";
import { test } from "node:test";
import {
  diffLines,
  fixView,
  hypotheses,
  hypothesisTone,
  sandbox,
} from "../src/features/agent/evidence.ts";

const BEFORE =
  '{\n  "DB_HOST": "h",\n  "DB_NAME": "app_prod",\n  "DB_USER": "u"\n}\n';
const AFTER =
  '{\n  "DB_HOST": "h",\n  "DB_NAME": "app_return",\n  "DB_USER": "u"\n}';

const DETAIL = {
  proposal: {
    cause: "DB 이름이 달라요",
    impact: "대상 DB로 연결해요",
    verification: "다시 읽어 확인했어요",
    changes: [{ key: "DB_NAME", value: "app_return", before: "app_prod" }],
  },
  hypotheses: [
    {
      id: "host",
      title: "DB 주소가 틀렸다",
      key: "DB_HOST",
      status: "invalidated",
      evidence: "TCP 연결은 됐어요.",
    },
    {
      id: "name",
      title: "DB 이름이 틀렸다",
      key: "DB_NAME",
      status: "validated",
      evidence: "DB 가 없음",
    },
    {
      id: "password",
      title: "비밀번호가 틀렸다",
      key: null,
      status: "inconclusive",
      evidence: "",
    },
    { id: "bad", title: "모르는 판정", key: null, status: "maybe" },
    { title: "id 없음", status: "validated" },
  ],
  sandbox: {
    runner: "opensandbox",
    image: "oneport/codex-sandbox:latest",
    model: "gpt-5.5",
    seconds: 19.4,
    steps: [
      { kind: "say", text: "파일을 읽고 비교할게요." },
      { kind: "run", text: "sed -n '1,200p' ./config/target.json", ok: true },
      { kind: "run", text: "false", ok: false },
      { kind: "edit", text: "config/target.json" },
      { kind: "think", text: "" },
      { kind: "shout", text: "x" },
    ],
    file: { path: "config/target.json", before: BEFORE, after: AFTER },
  },
};

test("hypotheses keep valid items, put causes first and mark what AI fixed", () => {
  const items = hypotheses(DETAIL);
  assert.deepEqual(
    items.map((h) => [h.id, h.status, h.fixed]),
    [
      ["name", "validated", true],
      ["password", "inconclusive", false],
      ["host", "invalidated", false],
    ],
  );
  assert.equal(hypothesisTone("validated"), "bad");
  assert.equal(hypothesisTone("inconclusive"), "idle");
  assert.deepEqual(hypotheses({}), []);
  const human = hypotheses({
    hypotheses: [
      { id: "password", title: "비밀번호", key: null, status: "validated" },
    ],
  });
  assert.equal(human[0].fixed, false);
  const unsure = hypotheses({
    proposal: DETAIL.proposal,
    hypotheses: [
      { id: "name", title: "DB 이름", key: "DB_NAME", status: "inconclusive" },
    ],
  });
  assert.equal(unsure[0].fixed, false);
});

test("sandbox steps drop unknown kinds and empty text", () => {
  const box = sandbox(DETAIL);
  assert.ok(box);
  assert.deepEqual(
    box.steps.map((s) => [s.kind, s.ok]),
    [
      ["say", null],
      ["run", true],
      ["run", false],
      ["edit", null],
    ],
  );
  assert.equal(box.seconds, 19.4);
  assert.equal(box.file?.after, AFTER);
  assert.equal(sandbox({}), null);
  assert.equal(sandbox({ sandbox: { steps: [] } })?.file, null);
});

test("diff marks only the changed line", () => {
  assert.deepEqual(diffLines(BEFORE, AFTER), [
    { kind: "same", text: "{" },
    { kind: "same", text: '  "DB_HOST": "h",' },
    { kind: "del", text: '  "DB_NAME": "app_prod",' },
    { kind: "add", text: '  "DB_NAME": "app_return",' },
    { kind: "same", text: '  "DB_USER": "u"' },
    { kind: "same", text: "}" },
  ]);
  assert.deepEqual(diffLines("a\nb\n", "b\nc\n"), [
    { kind: "del", text: "a" },
    { kind: "same", text: "b" },
    { kind: "add", text: "c" },
  ]);
});

test("fix view prefers the file diff and falls back to change rows", () => {
  const fix = fixView(DETAIL);
  assert.ok(fix);
  assert.equal(fix.cause, "DB 이름이 달라요");
  assert.equal(fix.checked, "다시 읽어 확인했어요");
  assert.equal(fix.diff?.filter((l) => l.kind !== "same").length, 2);
  const plain = fixView({ proposal: DETAIL.proposal });
  assert.equal(plain?.diff, null);
  assert.deepEqual(plain?.changes, [
    { key: "DB_NAME", before: "app_prod", after: "app_return" },
  ]);
  const same = fixView({
    proposal: DETAIL.proposal,
    sandbox: { file: { before: BEFORE, after: BEFORE } },
  });
  assert.equal(same?.diff, null);
  assert.equal(fixView({}), null);
  assert.equal(
    fixView({ sandbox: { file: { before: BEFORE, after: null } } }),
    null,
  );
});
