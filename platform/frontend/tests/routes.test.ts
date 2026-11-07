import assert from "node:assert/strict";
import { test } from "node:test";
import { ROUTE_EVENT } from "../src/app/routes.ts";
import {
  agentHash,
  controlHash,
  migrationHash,
  nodesHash,
  onRouteChange,
  readRoute,
  replaceHash,
  resourceHash,
  routeTitle,
} from "../src/app/routes.ts";

test("migrations is the default landing route", () => {
  for (const hash of ["", "#", "#migrations", "#apps", "#plan/x", "#unknown"])
    assert.deepEqual(readRoute(hash), { view: "migrations" }, hash);
});

test("job ids round trip through the hash", () => {
  const id = "forward 001/α";
  assert.equal(migrationHash(id), "#migrations/forward%20001%2F%CE%B1");
  assert.deepEqual(readRoute(migrationHash(id)), { view: "migration", id });
});

test("environments, security and team routes", () => {
  assert.deepEqual(readRoute("#environments"), { view: "environments" });
  assert.equal(routeTitle({ view: "environments" }, ""), "환경");
  assert.deepEqual(readRoute("#security"), { view: "security" });
  assert.deepEqual(readRoute("#team"), { view: "team" });
  assert.equal(routeTitle({ view: "team" }, "platform"), "platform 팀");
  assert.equal(
    routeTitle({ view: "migration", id: "forward_001" }, ""),
    "forward_001",
  );
});

test("agent console and run deep links", () => {
  assert.deepEqual(readRoute("#agent"), { view: "agent" });
  assert.equal(agentHash(), "#agent");
  assert.equal(agentHash("run-0a1b2c"), "#agent/run-0a1b2c");
  assert.deepEqual(readRoute(agentHash("run-0a1b2c")), {
    view: "agent",
    runId: "run-0a1b2c",
  });
  assert.equal(routeTitle({ view: "agent" }, ""), "AI 에이전트");
});

test("security and environment deep links carry their target", () => {
  assert.deepEqual(readRoute("#security/NET-03"), {
    view: "security",
    control: "NET-03",
  });
  assert.equal(controlHash("NET-03"), "#security/NET-03");
  assert.deepEqual(readRoute(resourceHash("aws", "rds")), {
    view: "environments",
    select: { envId: "aws", id: "rds" },
  });
  assert.deepEqual(readRoute("#environments/aws"), { view: "environments" });
});

test("node view links open the cluster or node panel", () => {
  assert.deepEqual(readRoute("#environments/nodes"), {
    view: "environments",
    nodes: {},
  });
  assert.equal(nodesHash("onprem"), "#environments/nodes/onprem");
  assert.deepEqual(readRoute(nodesHash("onprem", "worker-1")), {
    view: "environments",
    nodes: { envId: "onprem", node: "worker-1" },
  });
  assert.equal(nodesHash(), "#environments/nodes");
});

test("malformed escapes fall back to the raw segment instead of throwing", () => {
  assert.deepEqual(readRoute("#migrations/50%"), {
    view: "migration",
    id: "50%",
  });
  assert.deepEqual(readRoute("#environments/aws/%E0%A4%A"), {
    view: "environments",
    select: { envId: "aws", id: "%E0%A4%A" },
  });
  assert.deepEqual(readRoute("#environments/nodes/%/w%2"), {
    view: "environments",
    nodes: { envId: "%", node: "w%2" },
  });
  assert.deepEqual(readRoute("#security/%zz"), {
    view: "security",
    control: "%zz",
  });
  assert.deepEqual(readRoute("#costs/%"), { view: "costs", tab: "%" });
  assert.deepEqual(readRoute("#prepare/%"), {
    view: "prepare",
    requestId: "invalid-request",
  });
});

test("route listeners hear both hashchange and the replaceHash event", () => {
  const target = new EventTarget();
  let calls = 0;
  const stop = onRouteChange(target, () => calls++);
  target.dispatchEvent(new Event("hashchange"));
  target.dispatchEvent(new Event(ROUTE_EVENT));
  assert.equal(calls, 2);
  stop();
  target.dispatchEvent(new Event("hashchange"));
  target.dispatchEvent(new Event(ROUTE_EVENT));
  assert.equal(calls, 2);
});

test("replaceHash notifies route listeners without a hashchange", () => {
  const target = new EventTarget();
  const location = { hash: "#environments/aws/rds" };
  const history = {
    replaceState: (_: unknown, __: string, hash: string) => {
      location.hash = hash;
    },
  };
  Object.assign(target, { location, history });
  const saved = globalThis.window;
  globalThis.window = target as unknown as typeof globalThis.window;
  try {
    let calls = 0;
    const stop = onRouteChange(target, () => calls++);
    replaceHash("#environments");
    assert.equal(location.hash, "#environments");
    assert.equal(calls, 1);
    replaceHash("#environments");
    assert.equal(calls, 1);
    stop();
  } finally {
    globalThis.window = saved;
  }
});
