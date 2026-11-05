import assert from "node:assert/strict";
import { test } from "node:test";
import {
  controlHash,
  migrationHash,
  readRoute,
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
