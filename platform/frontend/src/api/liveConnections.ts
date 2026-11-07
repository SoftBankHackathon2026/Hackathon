import { request, type RequestOptions } from "./client.ts";
import type {
  ConnectionGateway,
  ConnectionSnapshot,
} from "../features/prepare/connectionTypes.ts";

type Transport = <T>(path: string, options?: RequestOptions) => Promise<T>;
type Capabilities = { available: boolean; reason?: string };
const PREFIX = "connections";
const OFFLINE =
  "연결 상태를 갱신하지 못했어요. 연결을 확인한 뒤 다시 시도하세요.";
const MESSAGES: Record<string, string> = {
  INVALID_CONFIRMATION_CODE:
    "코드가 일치하지 않아요. 단말의 8자리 코드를 다시 확인하세요.",
  CANDIDATE_CHANGED: "확인할 단말이 바뀌었어요. 최신 단말 정보를 확인하세요.",
  APPROVAL_UNAVAILABLE:
    "지금은 이 단말을 승인할 수 없어요. 기한과 상태를 확인하세요.",
  ALREADY_CONSUMED:
    "이미 단말이 접수한 요청이에요. 단말 확인 단계에서 계속하세요.",
  ALREADY_REGISTERED: "이미 등록한 환경이에요. 등록 정보를 확인하세요.",
  TUNNEL_NOT_READY: "보안 연결이 준비되지 않았어요. 연결 상태를 확인하세요.",
  RECONNECT_UNAVAILABLE:
    "지금은 재접속을 요청할 수 없어요. 최신 연결 상태를 확인하세요.",
  TARGET_NOT_DISCOVERED:
    "선택한 환경을 찾을 수 없어요. 최신 후보에서 다시 선택하세요.",
  COLLECTION_PENDING:
    "환경정보를 수집하고 있어요. 결과가 도착할 때까지 기다려 주세요.",
  INVENTORY_CHANGED:
    "수집 결과나 연결이 바뀌었어요. 환경정보를 다시 수집하세요.",
  INVALID_REGISTRATION: "등록할 조직·프로젝트·환경 정보를 확인하세요.",
  AUTHENTICATION_BLOCKED: "인증 실패가 반복되어 연결 요청이 일시 차단되었어요.",
  REQUEST_RATE_LIMITED: "요청이 많아요. 잠시 후 다시 시도하세요.",
  REQUEST_LIMIT_UNAVAILABLE:
    "인증 상태를 확인할 수 없어요. 잠시 후 다시 시도하세요.",
  CONNECTION_UNAVAILABLE:
    "연결 실행 환경이 준비되지 않았어요. 관리자에게 확인해 주세요.",
  CONNECTION_PERMISSION_DENIED: "이 연결을 변경할 권한이 없어요.",
  CONNECTION_NOT_FOUND: "연결 요청을 찾을 수 없어요.",
  IDEMPOTENCY_CONFLICT: "요청 정보가 바뀌었어요. 새 명령을 복사해 주세요.",
  INVALID_TEXT: "등록할 이름과 프로젝트의 입력 길이를 확인하세요.",
};
function userError(error: unknown) {
  const message = error instanceof Error ? error.message : "";
  return new Error(
    MESSAGES[message] ??
      (message && !/^[A-Z_]+$/.test(message)
        ? message
        : "요청을 처리하지 못했어요. 최신 연결 상태를 확인하고 다시 시도하세요."),
  );
}

function snapshots(value: unknown): ConnectionSnapshot[] {
  if (!Array.isArray(value)) throw new Error(OFFLINE);
  for (const s of value) {
    if (
      !s ||
      typeof s !== "object" ||
      typeof s.id !== "string" ||
      !/^[A-Za-z0-9_-]{22}$/.test(s.id) ||
      typeof s.organization !== "string" ||
      !Number.isSafeInteger(s.revision) ||
      !Number.isSafeInteger(s.serverNow) ||
      !Number.isSafeInteger(s.observedAt) ||
      !Array.isArray(s.discovered) ||
      !Array.isArray(s.collected) ||
      !Array.isArray(s.events) ||
      !s.permissions ||
      typeof s.permissions.approveDevice !== "boolean" ||
      typeof s.permissions.registerEnvironment !== "boolean" ||
      typeof s.permissions.revoke !== "boolean"
    )
      throw new Error(OFFLINE);
  }
  return value;
}

/** One authenticated view owns its cache; no secrets or cross-session storage. */
export function createLiveConnections(
  transport: Transport = request,
  pollMs = 5_000,
): ConnectionGateway {
  let items: readonly ConnectionSnapshot[] = [];
  const listeners = new Set<() => void>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  let generation = 0;
  let lifecycle = 0;
  let writes = 0;
  let paused = false;
  const emit = () => {
    items = [...items];
    listeners.forEach((listener) => listener());
  };
  const apply = (incoming: ConnectionSnapshot[]) => {
    items = incoming.map((s) => {
      const old = items.find((item) => item.id === s.id);
      return old && old.revision > s.revision ? old : s;
    });
  };
  const refresh = async () => {
    const current = ++generation;
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    try {
      const capability = await transport<Capabilities>(
        `${PREFIX}/capabilities`,
        { signal },
      );
      if (signal.aborted || current !== generation) return;
      if (typeof capability?.available !== "boolean") throw new Error(OFFLINE);
      paused = !capability.available;
      if (capability.available) {
        const data = await transport<unknown>(PREFIX, { signal });
        if (signal.aborted || current !== generation) return;
        apply(snapshots(data));
      }
      gateway.available = capability.available;
      gateway.reason = capability.available
        ? undefined
        : (capability.reason ?? "연결 서비스 준비가 필요해요.");
      emit();
    } catch {
      if (signal.aborted || current !== generation) return;
      gateway.available = false;
      gateway.reason = OFFLINE;
      // observedAt is deliberately preserved: stale snapshots cannot become fresh.
      emit();
      throw new Error(OFFLINE);
    }
  };
  const poll = () => {
    if (paused) return;
    const current = lifecycle;
    void (writes ? Promise.resolve() : refresh())
      .catch(() => {})
      .finally(() => {
        if (!paused && listeners.size && current === lifecycle)
          timer = setTimeout(poll, pollMs);
      });
  };
  const path = (id: string, operation: string) => {
    if (!/^[A-Za-z0-9_-]{22}$/.test(id))
      throw new Error("연결 요청을 확인하세요.");
    return `${PREFIX}/${id}/${operation}`;
  };
  const mutate = async (id: string, operation: string, body?: unknown) => {
    const current = lifecycle;
    writes++;
    generation++;
    controller?.abort();
    try {
      await transport(path(id, operation), {
        method: "POST",
        body: body ?? {},
      });
    } catch (error) {
      // Incorrect confirmation codes consume attempts on the server too.
      if (current === lifecycle) await refresh().catch(() => {});
      throw userError(error);
    } finally {
      writes--;
    }
    if (current === lifecycle) await refresh();
  };
  const gateway: ConnectionGateway = {
    mode: "live",
    available: false,
    reason: "연결 서비스 상태를 확인하고 있어요.",
    list: () => items,
    subscribe(listener) {
      listeners.add(listener);
      if (listeners.size === 1) {
        lifecycle++;
        poll();
      }
      return () => {
        listeners.delete(listener);
        if (!listeners.size) {
          clearTimeout(timer);
          controller?.abort();
          generation++;
          lifecycle++;
          items = [];
          paused = false;
          gateway.available = false;
          gateway.reason = "연결 서비스 상태를 확인하고 있어요.";
        }
      };
    },
    async create(_context, publicKey, operationId) {
      if (!gateway.available) throw new Error(gateway.reason);
      const current = lifecycle;
      writes++;
      generation++;
      controller?.abort();
      // Organization is resolved from the authenticated server session.
      try {
        const result = await transport<unknown>(PREFIX, {
          method: "POST",
          body: { publicKey, operationId },
        });
        const s = snapshots([result])[0];
        if (current === lifecycle) {
          apply([s, ...items.filter((item) => item.id !== s.id)]);
          emit();
        }
        return s;
      } catch (error) {
        throw userError(error);
      } finally {
        writes--;
      }
    },
    cancel: (id) => mutate(id, "cancel"),
    approveDevice: (id, candidateDigest, code) =>
      mutate(id, "approve", { candidateDigest, code }),
    reject: (id) => mutate(id, "reject"),
    collect: (id, discoveredId) => mutate(id, "collect", { discoveredId }),
    register: (id, collectedId, inventoryDigest, target) =>
      mutate(id, "register", { collectedId, inventoryDigest, target }),
    reconnect: (id) => mutate(id, "reconnect"),
    revoke: (id) => mutate(id, "revoke"),
  };
  return gateway;
}
