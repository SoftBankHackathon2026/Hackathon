import assert from "node:assert/strict";
import { test } from "node:test";
import { setImmediate } from "node:timers/promises";
import { createLiveConnections } from "../src/api/liveConnections.ts";
import { createPreviewGateway } from "../src/features/prepare/previewGateway.ts";
import type { RequestOptions } from "../src/api/client.ts";

async function fixture() {
  const preview = createPreviewGateway();
  const s = await preview.create({ organization: "team" }, "public", "op");
  return {
    ...s,
    id: "a".repeat(22),
    deployment: {
      bootstrapUrl: "https://connect.example.com/i",
      releaseVersion: "test-v1",
    },
  };
}
async function until(predicate: () => boolean) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await setImmediate();
  }
  assert.fail("expected state was not reached");
}

test("live gateway uses cookie API and sends only public material on creation", async (t) => {
  const s = await fixture();
  const calls: { path: string; options?: RequestOptions }[] = [];
  const gateway = createLiveConnections(
    async <T>(path: string, options?: RequestOptions): Promise<T> => {
      calls.push({ path, options });
      if (path.endsWith("capabilities")) return { available: true } as T;
      return (options?.method === "POST" ? s : []) as T;
    },
    60_000,
  );
  const unsubscribe = gateway.subscribe(() => {});
  t.after(unsubscribe);
  await until(() => gateway.available);
  await gateway.create(
    { organization: "untrusted-browser-team" },
    "PUBLIC_KEY",
    "OPERATION_ID",
  );
  const creation = calls.find((c) => c.options?.method === "POST")!;
  assert.equal(creation.path, "connections");
  assert.deepEqual(creation.options?.body, {
    publicKey: "PUBLIC_KEY",
    operationId: "OPERATION_ID",
  });
  assert.equal(gateway.list()[0].id, s.id);
  unsubscribe();
  assert.deepEqual(gateway.list(), []);
  assert.equal(gateway.available, false);
});

test("a rejected code refreshes authoritative attempt count", async (t) => {
  let s = await fixture();
  const gateway = createLiveConnections(
    async <T>(path: string, options?: RequestOptions): Promise<T> => {
      if (path.endsWith("capabilities")) return { available: true } as T;
      if (options?.method === "POST") {
        s = { ...s, revision: s.revision + 1, attemptsLeft: 4 };
        throw new Error("INVALID_CONFIRMATION_CODE");
      }
      return [s] as T;
    },
    60_000,
  );
  t.after(gateway.subscribe(() => {}));
  await until(() => gateway.available);
  await assert.rejects(
    () => gateway.approveDevice(s.id, "digest", "WRONG123"),
    /일치하지/,
  );
  assert.equal(gateway.list()[0].attemptsLeft, 4);
});

test("failed refresh never gives stale records a new observation time", async (t) => {
  const s = await fixture();
  let offline = false;
  const gateway = createLiveConnections(
    async <T>(path: string, options?: RequestOptions): Promise<T> => {
      if (offline) throw new Error("offline");
      if (path.endsWith("capabilities")) return { available: true } as T;
      if (options?.method === "POST") {
        offline = true;
        return {} as T;
      }
      return [s] as T;
    },
    60_000,
  );
  t.after(gateway.subscribe(() => {}));
  await until(() => gateway.available);
  await assert.rejects(() => gateway.reconnect(s.id));
  assert.equal(gateway.available, false);
  assert.equal(gateway.list()[0].observedAt, s.observedAt);
});

test("missing deployment capability prevents issuing a request", async (t) => {
  let posts = 0;
  const gateway = createLiveConnections(
    async <T>(path: string, options?: RequestOptions): Promise<T> => {
      if (options?.method === "POST") posts++;
      return (
        path.endsWith("capabilities")
          ? { available: false, reason: "배포 준비 필요" }
          : []
      ) as T;
    },
    60_000,
  );
  t.after(gateway.subscribe(() => {}));
  await until(() => gateway.reason === "배포 준비 필요");
  await assert.rejects(() =>
    gateway.create({ organization: "team" }, "public", "op"),
  );
  assert.equal(posts, 0);
});

test("unsubscribed responses cannot repopulate a later view", async () => {
  const s = await fixture();
  let resolveList: (data: unknown) => void = () => {};
  const gateway = createLiveConnections(async <T>(path: string): Promise<T> => {
    if (path.endsWith("capabilities")) return { available: true } as T;
    return (await new Promise((resolve) => {
      resolveList = resolve;
    })) as T;
  });
  const unsubscribe = gateway.subscribe(() => {});
  unsubscribe();
  resolveList([s]);
  await setImmediate();
  assert.equal(gateway.available, false);
  assert.deepEqual(gateway.list(), []);
});

test("disabled service preserves its configuration reason when list is unavailable", async (t) => {
  const gateway = createLiveConnections(async <T>(path: string): Promise<T> => {
    if (path.endsWith("capabilities"))
      return { available: false, reason: "배포 준비 필요" } as T;
    throw new Error("503 service unavailable");
  });
  t.after(gateway.subscribe(() => {}));
  await until(() => gateway.reason === "배포 준비 필요");
  assert.equal(gateway.available, false);
  assert.deepEqual(gateway.list(), []);
});
