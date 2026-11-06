import type { ConnectionGateway } from "../features/prepare/connectionTypes";

const unavailable = async (): Promise<never> => {
  throw new Error(
    "새 연결 서비스가 아직 제공되지 않아요. 등록 요청은 전송하지 않았어요.",
  );
};
const EMPTY: ReturnType<ConnectionGateway["list"]> = [];
// No fallback to the legacy bearer-token scripts. Enable only after /v1 session,
// capability, snapshot and cancellation contracts have been agreed and tested.
export const unavailableConnections: ConnectionGateway = {
  mode: "unavailable",
  available: false,
  reason:
    "연결 서비스 준비 중이에요. 환경을 구성해 볼 수 있지만 명령 발급과 실제 연결은 아직 사용할 수 없어요.",
  list: () => EMPTY,
  subscribe: () => () => {},
  create: unavailable,
  cancel: unavailable,
  approveDevice: unavailable,
  reject: unavailable,
  inspect: unavailable,
  approvePlan: unavailable,
  reconnect: unavailable,
  revoke: unavailable,
};
