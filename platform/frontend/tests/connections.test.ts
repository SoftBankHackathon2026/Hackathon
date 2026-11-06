import assert from "node:assert/strict";
import { test } from "node:test";
import { ml_dsa65 } from "@noble/post-quantum/ml-dsa.js";
import {
  base64url,
  createEnrollmentMaterial,
  enrollmentCommand,
} from "../src/features/prepare/enrollmentCrypto.ts";
import {
  canApproveDevice,
  canApprovePlan,
  connectionStatus,
  emptyTarget,
  remainingSeconds,
  stepFor,
} from "../src/features/prepare/model.ts";
import {
  createPreviewGateway,
  PREVIEW_CODE,
} from "../src/features/prepare/previewGateway.ts";
import { readRoute } from "../src/app/routes.ts";
const target = { ...emptyTarget("팀"), project: "주문 이관", name: "busan" };
async function request() {
  const gateway = createPreviewGateway();
  const snapshot = await gateway.create(target, "public-key", "test-operation");
  return { gateway, id: snapshot.id };
}

test("independent registration, approval and tunnel states never imply completed preparation", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "pending");
  let s = gateway.list()[0];
  assert.equal(stepFor(s), 2);
  assert.equal(s.registration, "CONSUMED");
  assert.equal(s.tunnel, "NONE");
  assert.equal(remainingSeconds(s.approvalExpiresAt, s.serverNow), 300);
  await gateway.approveDevice(id, s.candidate!.digest, PREVIEW_CODE);
  s = gateway.list()[0];
  assert.equal(s.planState, "NONE");
  assert.equal(s.job, "NONE");
  assert.equal(stepFor(s), 3);
  await assert.rejects(() => gateway.inspect(id, "busan-production"));
});

test("five code failures reject candidate, never silently approve", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "pending");
  for (let i = 0; i < 5; i++)
    await assert.rejects(() =>
      gateway.approveDevice(
        id,
        gateway.list()[0].candidate!.digest,
        "ZZZZ9999",
      ),
    );
  const s = gateway.list()[0];
  assert.equal(s.attemptsLeft, 0);
  assert.equal(s.device, "REJECTED");
  assert.equal(canApproveDevice(s, Date.now()), false);
});

test("candidate substitution and expired approval are rejected", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "pending");
  const s = gateway.list()[0];
  await assert.rejects(() =>
    gateway.approveDevice(id, "different-digest", PREVIEW_CODE),
  );
  assert.equal(
    canApproveDevice({ ...s, approvalExpiresAt: s.serverNow - 1 }, s.serverNow),
    false,
  );
  assert.equal(
    canApproveDevice(
      { ...s, permissions: { ...s.permissions, approveDevice: false } },
      s.serverNow,
    ),
    false,
  );
});

test("context must be explicitly selected and approved plan hash cannot change", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "connecting");
  await gateway.simulate(id, "tunnel");
  await assert.rejects(() => gateway.inspect(id));
  await gateway.inspect(id, "busan-production");
  const s = gateway.list()[0];
  assert.equal(s.selectedContext, "busan-production");
  assert.equal(canApprovePlan(s, s.serverNow), true);
  await assert.rejects(() => gateway.approvePlan(id, "substituted"));
  assert.equal(gateway.list()[0].job, "NONE");
  assert.equal(
    canApprovePlan({ ...s, tunnel: "DEGRADED" }, s.serverNow),
    false,
  );
  assert.equal(
    canApprovePlan({ ...s, targetIdentity: "other" }, s.serverNow),
    false,
  );
  assert.equal(canApprovePlan(s, s.plan!.expiresAt), false);
});

test("unknown results and partial failure do not claim successful preparation", async () => {
  for (const scenario of ["unknown", "partial"] as const) {
    const { gateway, id } = await request();
    await gateway.simulate(id, scenario);
    const s = gateway.list()[0];
    assert.notEqual(connectionStatus(s).tone, "ok");
    assert.equal(stepFor(s), 5);
    await assert.rejects(() => gateway.simulate(id, "complete"));
  }
});

test("tunnel recovery keeps completed setup separate and requires a new generation", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "degraded");
  const epoch = gateway.list()[0].epoch;
  assert.equal(connectionStatus(gateway.list()[0]).tone, "bad");
  await gateway.reconnect(id);
  assert.equal(gateway.list()[0].tunnel, "CONNECTING");
  assert.notEqual(connectionStatus(gateway.list()[0]).tone, "ok");
  await gateway.simulate(id, "tunnel");
  assert.equal(gateway.list()[0].epoch, epoch + 1);
});

test("revocation distinguishes server block from unconfirmed remote cleanup", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "active");
  assert.equal(connectionStatus(gateway.list()[0]).tone, "ok");
  await gateway.revoke(id);
  const s = gateway.list()[0];
  assert.equal(s.device, "REVOKED");
  assert.equal(stepFor(s), 5);
  assert.deepEqual(s.cleanup, {
    server: true,
    gateway: true,
    resources: false,
    local: false,
  });
});

test("registration cutoff is exactly server deadline; approval remains independent", () => {
  assert.equal(remainingSeconds(60_000, 59_999), 1);
  assert.equal(remainingSeconds(60_000, 60_000), 0);
  assert.equal(remainingSeconds(60_000, 60_001), 0);
});

test("cancel only unused enrollment; consumed candidates require explicit rejection", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "pending");
  await assert.rejects(() => gateway.cancel(id));
  assert.equal(gateway.list().length, 1);
  const second = await gateway.create(target, "public", "second");
  await gateway.cancel(second.id);
  assert.equal(gateway.list().length, 1);
});

test("browser key material reconstructs ML-DSA-65 public key; seed is 43 characters", () => {
  const material = createEnrollmentMaterial();
  const reconstructed = ml_dsa65.keygen(material.seed);
  assert.equal(base64url(reconstructed.publicKey), material.publicKey);
  assert.equal(base64url(material.seed).length, 43);
  assert.equal(reconstructed.publicKey.length, 1952);
  material.seed.fill(0);
  reconstructed.secretKey.fill(0);
});

test("command generator rejects shell injection and uses seed only in Bash environment", () => {
  const seed = new Uint8Array(32).fill(42),
    id = "a".repeat(22);
  const cmd = enrollmentCommand("https://connect.example.com/i", id, seed);
  assert.ok(cmd.length <= 256);
  assert.match(cmd, /\| MIGRATION_ENROLL='/);
  assert.ok(!cmd.slice(0, cmd.indexOf("|")).includes(base64url(seed)));
  for (const url of [
    "http://x/i",
    "https://x/i?seed=x",
    "https://x/$(id)",
    "https://u:p@x/i",
  ])
    assert.throws(() => enrollmentCommand(url, id, seed));
  assert.throws(() => enrollmentCommand("https://x/i", "';id;'", seed));
});

test("preview persistence contains snapshots, never enrollment seed or private key", async () => {
  let stored = "";
  const gateway = createPreviewGateway({
    getItem: () => "[]",
    setItem: (_, value) => {
      stored = value;
    },
  });
  await gateway.create(target, "PUBLIC_ONLY", "idempotency");
  assert.doesNotMatch(
    stored,
    /PUBLIC_ONLY|seed|privateKey|secretKey|MIGRATION_ENROLL/,
  );
  const restored = createPreviewGateway({
    getItem: () => stored,
    setItem: () => {},
  });
  assert.equal(restored.list()[0].target.name, target.name);
});

test("request deep links carry only public id and tolerate malformed encoding", () => {
  assert.deepEqual(readRoute("#prepare/abc"), {
    view: "prepare",
    requestId: "abc",
  });
  assert.deepEqual(readRoute("#prepare/%E0"), {
    view: "prepare",
    requestId: "invalid-request",
  });
});

test("AWS and GCP plans scope data, secrets and decrypt separately without long lived keys", async () => {
  for (const kind of ["aws", "gcp"] as const) {
    const gateway = createPreviewGateway();
    const s = await gateway.create(
      { ...target, kind, region: "seoul" },
      "public",
      kind,
    );
    await gateway.simulate(s.id, "plan");
    const plan = gateway.list()[0].plan!;
    assert.ok(plan.privileges.includes("secrets.read"));
    assert.ok(plan.privileges.includes("decrypt.use"));
    assert.equal(JSON.parse(plan.definition).long_lived_keys, false);
    assert.ok(!plan.privileges.includes("migration.write"));
  }
});
