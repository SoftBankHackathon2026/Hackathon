import assert from "node:assert/strict";
import { test } from "node:test";
import {
  RELOAD_COOLDOWN_MS,
  RELOAD_KEY,
  installStaleChunkReload,
  shouldReload,
  type ReloadStore,
} from "../src/app/staleChunk.ts";

function memoryStore(): ReloadStore & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
  };
}

test("first stale chunk error reloads and remembers when", () => {
  const store = memoryStore();
  assert.equal(shouldReload(store, 1_000_000), true);
  assert.equal(store.data.get(RELOAD_KEY), "1000000");
});

test("a second error inside the cooldown does not reload again", () => {
  const store = memoryStore();
  assert.equal(shouldReload(store, 1_000_000), true);
  assert.equal(shouldReload(store, 1_000_000 + RELOAD_COOLDOWN_MS - 1), false);
  assert.equal(store.data.get(RELOAD_KEY), "1000000");
  assert.equal(shouldReload(store, 1_000_000 + RELOAD_COOLDOWN_MS), true);
});

test("without usable storage it never reloads", () => {
  assert.equal(shouldReload(null, 1), false);
  const broken: ReloadStore = {
    getItem: () => null,
    setItem: () => {
      throw new Error("quota");
    },
  };
  assert.equal(shouldReload(broken, 1), false);
});

test("garbage in storage is treated as no earlier reload", () => {
  const store = memoryStore();
  store.data.set(RELOAD_KEY, "nope");
  assert.equal(shouldReload(store, 5), true);
});

function fakeWindow(storage: () => ReloadStore) {
  const listeners = new Map<string, (event: Event) => void>();
  let reloads = 0;
  const target = {
    addEventListener: (type: string, listener: (event: Event) => void) =>
      void listeners.set(type, listener),
    get sessionStorage() {
      return storage();
    },
    location: { reload: () => void reloads++ },
  };
  const fire = () => {
    let prevented = false;
    const event = { preventDefault: () => void (prevented = true) } as Event;
    listeners.get("vite:preloadError")?.(event);
    return prevented;
  };
  return { target, fire, reloads: () => reloads, listeners };
}

test("preload error reloads once and suppresses the error", () => {
  const store = memoryStore();
  const win = fakeWindow(() => store);
  let now = 50_000;
  installStaleChunkReload(win.target, () => now);
  assert.deepEqual([...win.listeners.keys()], ["vite:preloadError"]);
  assert.equal(win.fire(), true);
  assert.equal(win.reloads(), 1);
  now += 1_000;
  assert.equal(win.fire(), false);
  assert.equal(win.reloads(), 1);
});

test("blocked storage lets the error through without reloading", () => {
  const win = fakeWindow(() => {
    throw new Error("SecurityError");
  });
  installStaleChunkReload(win.target, () => 1);
  assert.equal(win.fire(), false);
  assert.equal(win.reloads(), 0);
});
