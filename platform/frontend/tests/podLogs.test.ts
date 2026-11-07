import assert from "node:assert/strict";
import { test } from "node:test";
import {
  atBottom,
  highlight,
  levelCounts,
  logFileName,
  logLevel,
  logText,
  logTime,
  matches,
  canScroll,
  newLineCount,
} from "../src/features/environments/podLogs.ts";

const line = (message: string, at: string | null = null) => ({ at, message });

test("logLevel spots errors and warnings without flagging normal lines", () => {
  assert.equal(logLevel("ERROR upstream timeout"), "error");
  assert.equal(logLevel("level=error msg=boom"), "error");
  assert.equal(logLevel("panic: runtime error"), "error");
  assert.equal(logLevel("java.lang.IllegalStateException: x"), "error");
  assert.equal(logLevel("Exception in thread main"), "error");
  assert.equal(logLevel("ValueError: bad"), "error");
  assert.equal(logLevel("connection refused"), "error");
  assert.equal(logLevel("E1107 12:00:00 controller failed"), "error");
  assert.equal(logLevel("F1107 fatal"), "error");
  assert.equal(logLevel("WARN slow query"), "warn");
  assert.equal(logLevel("level=warning disk"), "warn");
  assert.equal(logLevel("W1107 12:00:00 retrying"), "warn");
  assert.equal(logLevel("GET /healthz 200 2ms"), null);
  assert.equal(logLevel("I1107 12:00:00 started"), null);
  assert.equal(logLevel("errors=0 warnings=0"), null);
  assert.equal(logLevel("Every1107 day"), null);
  assert.equal(
    logLevel(
      "[2026-11-07 09:01:41,275] INFO [QuorumController id=0] failAll(NotControllerException): failing write",
    ),
    null,
  );
  assert.equal(logLevel('time=1 level=info msg="retry failed once"'), null);
  assert.equal(logLevel("I1107 12:00:00 lease failed, retrying"), null);
  assert.equal(logLevel("2026-11-07 [WARN] disk 80%"), "warn");
  assert.equal(logLevel("ERRO[0001] boom"), "error");
  assert.equal(logLevel("level=unknown something failed"), "error");
  assert.equal(logLevel("Information about failed jobs"), "error");
  assert.equal(logLevel("level=warning job failed"), "warn");
  assert.equal(logLevel(`${"x".repeat(90)} INFO but it failed`), "error");
});

test("matches ignores case and blank queries", () => {
  assert.equal(matches("GET /Orders 200", "orders"), true);
  assert.equal(matches("GET /orders 200", "  "), true);
  assert.equal(matches("GET /orders 200", "users"), false);
  assert.equal(matches("GET /orders 200", " orders "), true);
});

test("highlight splits every hit and keeps original casing", () => {
  assert.deepEqual(highlight("Err err ok", "err"), [
    { text: "Err", hit: true },
    { text: " ", hit: false },
    { text: "err", hit: true },
    { text: " ok", hit: false },
  ]);
  assert.deepEqual(highlight("plain", ""), [{ text: "plain", hit: false }]);
  assert.deepEqual(highlight("abc", "zz"), [{ text: "abc", hit: false }]);
  assert.deepEqual(highlight("xx", "x"), [
    { text: "x", hit: true },
    { text: "x", hit: true },
  ]);
});

test("logTime shows only the clock for today and adds the day otherwise", () => {
  const now = Date.parse("2026-11-07T12:00:00");
  const today = logTime("2026-11-07T09:05:03", now);
  const earlier = logTime("2026-11-05T09:05:03", now);
  assert.match(today, /09:05:03$/);
  assert.equal(today.length, 8);
  assert.match(earlier, /09:05:03$/);
  assert.ok(earlier.length > today.length);
  assert.equal(logTime(null, now), "");
  assert.equal(logTime("not a date", now), "");
});

test("logText keeps timestamps for copy and download", () => {
  assert.equal(
    logText([line("a", "2026-11-07T00:00:00Z"), line("b")]),
    "2026-11-07T00:00:00Z a\nb",
  );
  assert.equal(logText([]), "");
});

test("logFileName names previous runs separately", () => {
  assert.equal(logFileName("p-0", "app", false), "p-0_app.log");
  assert.equal(logFileName("p-0", "app", true), "p-0_app_previous.log");
});

test("levelCounts tallies errors and warnings", () => {
  assert.deepEqual(
    levelCounts([line("ERROR a"), line("WARN b"), line("ok"), line("fatal c")]),
    { error: 2, warn: 1 },
  );
});

test("atBottom allows a little slack", () => {
  assert.equal(
    atBottom({ scrollTop: 476, scrollHeight: 1000, clientHeight: 500 }),
    true,
  );
  assert.equal(
    atBottom({ scrollTop: 475, scrollHeight: 1000, clientHeight: 500 }),
    false,
  );
  assert.equal(
    atBottom({ scrollTop: 0, scrollHeight: 400, clientHeight: 500 }),
    true,
  );
});

test("newLineCount counts lines after the last one already shown", () => {
  const a = line("a", "t1");
  const b = line("b", "t2");
  const c = line("c", "t3");
  const d = line("d", "t4");
  assert.equal(newLineCount([], [a, b]), 0);
  assert.equal(newLineCount([a, b], [a, b, c, d]), 2);
  assert.equal(newLineCount([a, b], [b, c]), 1);
  assert.equal(newLineCount([a, b], [a, b]), 0);
  assert.equal(newLineCount([a], [c, d]), 2);
  assert.equal(newLineCount([b], [b, line("b", "t2"), c]), 1);
});

test("canScroll knows when an element can still move in the wheel direction", () => {
  const box = (scrollTop: number) => ({
    scrollTop,
    scrollHeight: 1000,
    clientHeight: 400,
  });
  assert.equal(canScroll(box(0), 10), true);
  assert.equal(canScroll(box(0), -10), false);
  assert.equal(canScroll(box(600), 10), false);
  assert.equal(canScroll(box(599), 10), true);
  assert.equal(canScroll(box(600), -10), true);
  assert.equal(
    canScroll({ scrollTop: 0, scrollHeight: 400, clientHeight: 400 }, 10),
    false,
  );
  assert.equal(
    canScroll({ scrollTop: 0, scrollHeight: 400, clientHeight: 400 }, -10),
    false,
  );
});
