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
  canRegister,
  connectionStatus,
  emptyTarget,
  remainingSeconds,
  stepFor,
  STEPS,
} from "../src/features/prepare/model.ts";
import {
  createPreviewGateway,
  PREVIEW_CODE,
} from "../src/features/prepare/previewGateway.ts";
import { readRoute } from "../src/app/routes.ts";
const context = { organization: "팀" };
async function request() {
  const gateway = createPreviewGateway();
  const s = await gateway.create(context, "public", "operation");
  return { gateway, id: s.id };
}
function metadata(
  s: ReturnType<ReturnType<typeof createPreviewGateway>["list"]>[number],
) {
  return {
    ...emptyTarget(s.organization),
    project: "주문 서비스",
    name: "운영 환경",
    kind: s.collected[0].kind,
    region: s.collected[0].region,
  };
}
test("two stages collect before any environment metadata is registered", async () => {
  assert.deepEqual(STEPS, ["환경정보 수집", "환경등록"]);
  const { gateway, id } = await request();
  assert.equal(gateway.list()[0].target, null);
  assert.equal(stepFor(gateway.list()[0]), 0);
  await gateway.simulate(id, "pending");
  let s = gateway.list()[0];
  assert.equal(stepFor(s), 0);
  assert.equal(remainingSeconds(s.approvalExpiresAt, s.serverNow), 300);
  await gateway.approveDevice(id, s.candidate!.digest, PREVIEW_CODE);
  assert.equal(stepFor(gateway.list()[0]), 0);
  await gateway.simulate(id, "tunnel");
  assert.equal(stepFor(gateway.list()[0]), 0);
  assert.equal(gateway.list()[0].target, null);
  await gateway.collect(id, "onprem-busan");
  s = gateway.list()[0];
  assert.equal(stepFor(s), 1);
  assert.equal(s.environmentId, undefined);
  assert.equal(s.target, null);
});
test("collection requires approved terminal, ready tunnel and explicit discovered context", async () => {
  const { gateway, id } = await request();
  await assert.rejects(() => gateway.collect(id, "onprem-busan"));
  await gateway.simulate(id, "connecting");
  await assert.rejects(() => gateway.collect(id, "onprem-busan"));
  await gateway.simulate(id, "tunnel");
  await assert.rejects(() => gateway.collect(id, ""));
  await assert.rejects(() => gateway.collect(id, "other-cluster"));
  assert.equal(canRegister(gateway.list()[0]), false);
});
test("environment registration rejects incomplete or substituted inventory and organization", async () => {
  const { gateway, id } = await request();
  await assert.rejects(() =>
    gateway.register(id, "onprem-busan", "fake", {
      ...emptyTarget("팀"),
      project: "앱",
      name: "환경",
    }),
  );
  await gateway.simulate(id, "collected");
  const s = gateway.list()[0],
    target = metadata(s);
  await assert.rejects(() =>
    gateway.register(id, s.collected[0].id, "changed-digest", target),
  );
  await assert.rejects(() =>
    gateway.register(id, "other-environment", s.inventoryDigest!, target),
  );
  await assert.rejects(() =>
    gateway.register(id, s.collected[0].id, s.inventoryDigest!, {
      ...target,
      organization: "다른 조직",
    }),
  );
  await assert.rejects(() =>
    gateway.register(id, s.collected[0].id, s.inventoryDigest!, {
      ...target,
      kind: "aws",
      region: "seoul",
    }),
  );
  await assert.rejects(() =>
    gateway.register(id, s.collected[0].id, s.inventoryDigest!, {
      ...target,
      name: "",
    }),
  );
  assert.equal(gateway.list()[0].environmentId, undefined);
});
test("registration saves metadata once without creating installation or migration work", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "collected");
  const s = gateway.list()[0],
    target = metadata(s);
  await gateway.register(id, s.collected[0].id, s.inventoryDigest!, target);
  const registered = gateway.list()[0];
  assert.equal(registered.target!.name, target.name);
  assert.equal(connectionStatus(registered).label, "환경등록 완료");
  assert.equal(
    "plan" in registered ||
      "job" in registered ||
      "scopes" in registered.target!,
    false,
  );
  await assert.rejects(() =>
    gateway.register(id, s.collected[0].id, s.inventoryDigest!, target),
  );
});
test("five wrong terminal codes close candidate without moving to registration", async () => {
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
  assert.equal(stepFor(s), 0);
  assert.equal(canApproveDevice(s, Date.now()), false);
});
test("approval checks candidate, permission and separate five minute deadline", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "pending");
  const s = gateway.list()[0];
  await assert.rejects(() =>
    gateway.approveDevice(id, "other-candidate", PREVIEW_CODE),
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
test("stale connection or missing registration permission blocks registration", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "collected");
  const s = gateway.list()[0];
  assert.equal(canRegister(s), true);
  assert.equal(canRegister({ ...s, tunnel: "DEGRADED" }), false);
  assert.equal(
    canRegister({
      ...s,
      permissions: { ...s.permissions, registerEnvironment: false },
    }),
    false,
  );
});
test("revocation during asynchronous collection discards its result", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "connecting");
  await gateway.simulate(id, "tunnel");
  const collecting = gateway.collect(id, "onprem-busan");
  await gateway.revoke(id);
  await assert.rejects(() => collecting);
  assert.equal(gateway.list()[0].collected.length, 0);
});
test("reconnecting a registered environment retains registration and changes key generation", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "degraded");
  const s = gateway.list()[0];
  const environmentId = s.environmentId;
  await gateway.reconnect(id);
  await gateway.simulate(id, "tunnel");
  assert.equal(gateway.list()[0].environmentId, environmentId);
  assert.equal(gateway.list()[0].epoch, s.epoch + 1);
});
test("revocation distinguishes server block from unconfirmed remote cleanup", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "registered");
  await gateway.revoke(id);
  assert.deepEqual(gateway.list()[0].cleanup, {
    server: true,
    gateway: true,
    resources: false,
    local: false,
  });
  assert.notEqual(connectionStatus(gateway.list()[0]).tone, "ok");
});
test("AWS and GCP are inferred from selected collected records and not freely relabeled", async () => {
  for (const source of ["aws-seoul", "gcp-seoul"]) {
    const { gateway, id } = await request();
    await gateway.simulate(id, "connecting");
    await gateway.simulate(id, "tunnel");
    await gateway.collect(id, source);
    const s = gateway.list()[0];
    await gateway.register(id, source, s.inventoryDigest!, metadata(s));
    assert.equal(
      gateway.list()[0].target!.kind,
      source === "aws-seoul" ? "aws" : "gcp",
    );
  }
});
test("registration cutoff is exactly the server deadline", () => {
  assert.equal(remainingSeconds(60_000, 59_999), 1);
  assert.equal(remainingSeconds(60_000, 60_000), 0);
  assert.equal(remainingSeconds(60_000, 60_001), 0);
});
test("consumed candidate cannot be cancelled as unused enrollment", async () => {
  const { gateway, id } = await request();
  await gateway.simulate(id, "pending");
  await assert.rejects(() => gateway.cancel(id));
  assert.equal(gateway.list().length, 1);
});
test("ML-DSA public key can be reconstructed with a 43-character encoded seed", () => {
  const material = createEnrollmentMaterial(),
    keys = ml_dsa65.keygen(material.seed);
  assert.equal(base64url(keys.publicKey), material.publicKey);
  assert.equal(base64url(material.seed).length, 43);
  material.seed.fill(0);
  keys.secretKey.fill(0);
});
test("command generator rejects injection and places seed only in Bash environment", () => {
  const seed = new Uint8Array(32).fill(42),
    id = "a".repeat(22),
    cmd = enrollmentCommand("https://connect.example.com/i", id, seed);
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
test("preview persistence excludes enrollment material and restores registered metadata", async () => {
  let stored = "";
  const gateway = createPreviewGateway({
    getItem: () => "[]",
    setItem: (_, value) => {
      stored = value;
    },
  });
  const s = await gateway.create(context, "PUBLIC_ONLY", "operation");
  await gateway.simulate(s.id, "registered");
  assert.doesNotMatch(
    stored,
    /PUBLIC_ONLY|seed|privateKey|secretKey|MIGRATION_ENROLL/,
  );
  assert.equal(
    createPreviewGateway({ getItem: () => stored, setItem: () => {} }).list()[0]
      .target!.name,
    "busan-production",
  );
});
test("request deep links contain public ids and tolerate malformed encoding", () => {
  assert.deepEqual(readRoute("#prepare/abc"), {
    view: "prepare",
    requestId: "abc",
  });
  assert.deepEqual(readRoute("#prepare/%E0"), {
    view: "prepare",
    requestId: "invalid-request",
  });
});
