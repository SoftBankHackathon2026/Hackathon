import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { OFFLINE_MESSAGE, request } from "../src/api/client.ts";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

test("network failures become a readable message", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  await assert.rejects(request("fleet"), { message: OFFLINE_MESSAGE });
});

test("aborted requests keep their abort error", async () => {
  const controller = new AbortController();
  controller.abort();
  const aborted = new DOMException("aborted", "AbortError");
  globalThis.fetch = async () => {
    throw aborted;
  };
  await assert.rejects(
    request("fleet", { signal: controller.signal }),
    aborted,
  );
});

test("successful responses are still parsed", async () => {
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ ok: true }), { status: 200 });
  assert.deepEqual(await request("fleet"), { ok: true });
});
