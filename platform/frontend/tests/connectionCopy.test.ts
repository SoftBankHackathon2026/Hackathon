import assert from "node:assert/strict";
import { test } from "node:test";
import {
  copyEnrollmentCommand,
  CopyFailure,
} from "../src/features/prepare/copyEnrollmentCommand.ts";
import { emptyTarget } from "../src/features/prepare/model.ts";
import { createPreviewGateway } from "../src/features/prepare/previewGateway.ts";

function clipboard(
  t: { after: (cleanup: () => void) => void },
  write: (
    items: { getType: (type: string) => Promise<Blob> }[],
  ) => Promise<void>,
) {
  const originalItem = Object.getOwnPropertyDescriptor(
    globalThis,
    "ClipboardItem",
  );
  const originalClipboard = Object.getOwnPropertyDescriptor(
    navigator,
    "clipboard",
  );
  Object.defineProperty(globalThis, "ClipboardItem", {
    configurable: true,
    value: class {
      values: Record<string, Promise<Blob>>;
      constructor(values: Record<string, Promise<Blob>>) {
        this.values = values;
      }
      getType(type: string) {
        return this.values[type];
      }
    },
  });
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { write },
  });
  t.after(() => {
    if (originalItem)
      Object.defineProperty(globalThis, "ClipboardItem", originalItem);
    else Reflect.deleteProperty(globalThis, "ClipboardItem");
    if (originalClipboard)
      Object.defineProperty(navigator, "clipboard", originalClipboard);
    else Reflect.deleteProperty(navigator, "clipboard");
  });
}
const target = {
  ...emptyTarget("조직"),
  project: "프로젝트",
  name: "클러스터",
};

test("clipboard write starts in click before asynchronous key generation or creation", async (t) => {
  const gateway = createPreviewGateway();
  const calls: string[] = [];
  clipboard(t, async (items) => {
    calls.push("clipboard");
    const text = await (await items[0].getType("text/plain")).text();
    assert.match(text, /^# Oneport/);
    assert.doesNotMatch(text, /MIGRATION_ENROLL|curl|seed/);
  });
  const original = gateway.create;
  gateway.create = async (...args) => {
    calls.push("create");
    assert.equal(args.length, 3);
    assert.ok(args[1].length > 2500); // Encoded ML-DSA-65 public key, not seed.
    return original(...args);
  };
  const result = copyEnrollmentCommand(gateway, target);
  assert.deepEqual(calls, ["clipboard"]);
  await result;
  assert.deepEqual(calls, ["clipboard", "create"]);
});

test("clipboard denial cancels only issued request and does not retain a ticket", async (t) => {
  const gateway = createPreviewGateway();
  clipboard(t, async (items) => {
    await items[0].getType("text/plain");
    throw new Error("denied");
  });
  await assert.rejects(
    () => copyEnrollmentCommand(gateway, target),
    (e) =>
      e instanceof CopyFailure &&
      !e.request &&
      e.message.includes("미사용 요청은 취소"),
  );
  assert.equal(gateway.list().length, 0);
});

test("uncertain cancellation exposes safe request state without claiming cleanup", async (t) => {
  const gateway = createPreviewGateway();
  gateway.cancel = async () => {
    throw new Error("offline");
  };
  clipboard(t, async (items) => {
    await items[0].getType("text/plain");
    throw new Error("denied");
  });
  await assert.rejects(
    () => copyEnrollmentCommand(gateway, target),
    (e) => {
      assert.ok(e instanceof CopyFailure);
      assert.equal(e.request?.id, gateway.list()[0].id);
      assert.match(e.message, /취소도 확인하지 못했어요/);
      return true;
    },
  );
});

test("replacement cannot issue new credentials until previous cancellation succeeds", async (t) => {
  const gateway = createPreviewGateway();
  const existing = await gateway.create(target, "public", "previous");
  gateway.cancel = async () => {
    throw new Error("candidate already consumed");
  };
  clipboard(t, async (items) => {
    await items[0].getType("text/plain");
  });
  await assert.rejects(() =>
    copyEnrollmentCommand(gateway, target, existing.id),
  );
  assert.equal(gateway.list().length, 1);
});

test("unavailable service does not access the clipboard or issue credentials", async () => {
  const gateway = createPreviewGateway();
  gateway.available = false;
  await assert.rejects(() => copyEnrollmentCommand(gateway, target));
  assert.equal(gateway.list().length, 0);
});

test("live copy uses release URL and keeps seed out of the request snapshot", async (t) => {
  const gateway = createPreviewGateway();
  gateway.mode = "live";
  const original = gateway.create;
  gateway.create = async (...args) => ({
    ...(await original(...args)),
    id: "a".repeat(22),
    deployment: {
      bootstrapUrl: "https://connect.example.com/i",
      releaseVersion: "test-v1",
    },
  });
  let copied = "";
  clipboard(t, async (items) => {
    copied = await (await items[0].getType("text/plain")).text();
  });
  const result = await copyEnrollmentCommand(gateway, target);
  assert.match(
    copied,
    /^curl -qfsS --proto '=https' https:\/\/connect.example.com\/i \| MIGRATION_ENROLL='/,
  );
  const seed = copied.match(
    /MIGRATION_ENROLL='[A-Za-z0-9_-]{22}\.([A-Za-z0-9_-]{43})'/,
  )?.[1];
  assert.ok(seed);
  assert.ok(!JSON.stringify(result).includes(seed));
  assert.ok(!JSON.stringify(gateway.list()).includes(seed));
});
