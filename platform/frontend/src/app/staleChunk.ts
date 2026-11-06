export const RELOAD_KEY = "oneport:stale-chunk-reload-at";
export const RELOAD_COOLDOWN_MS = 10_000;

export interface ReloadStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function shouldReload(store: ReloadStore | null, now: number): boolean {
  if (!store) return false;
  try {
    const last = Number(store.getItem(RELOAD_KEY));
    if (last > 0 && now - last < RELOAD_COOLDOWN_MS) return false;
    store.setItem(RELOAD_KEY, String(now));
    return true;
  } catch {
    return false;
  }
}

export interface ReloadTarget {
  addEventListener(type: string, listener: (event: Event) => void): void;
  readonly sessionStorage: ReloadStore;
  readonly location: { reload(): void };
}

function storageOf(target: ReloadTarget): ReloadStore | null {
  try {
    return target.sessionStorage;
  } catch {
    return null;
  }
}

export function installStaleChunkReload(
  target: ReloadTarget,
  clock: () => number = Date.now,
): void {
  target.addEventListener("vite:preloadError", (event) => {
    if (!shouldReload(storageOf(target), clock())) return;
    event.preventDefault();
    target.location.reload();
  });
}
