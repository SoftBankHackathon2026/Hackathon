import type {
  ConnectionGateway,
  ConnectionSnapshot,
  CollectionContext,
} from "./connectionTypes.ts";

export class CopyFailure extends Error {
  request?: ConnectionSnapshot;
  constructor(message: string, request?: ConnectionSnapshot) {
    super(message);
    this.request = request;
  }
}

export const COPY_TIMEOUT_MS = 15_000;
const CANCEL_TIMEOUT_MS = 5_000;
class CopyTimeout extends Error {}
function within<T>(work: Promise<T>, milliseconds: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new CopyTimeout()), milliseconds);
  });
  return Promise.race([work, timeout]).finally(() => clearTimeout(timer));
}

// Start the clipboard operation synchronously within the click. Its Blob is
// resolved later, preserving Safari's user activation across asynchronous work.
export async function copyEnrollmentCommand(
  gateway: ConnectionGateway,
  target: CollectionContext,
  previousId?: string,
) {
  if (!gateway.available)
    throw new CopyFailure(gateway.reason ?? "연결 서비스를 사용할 수 없어요.");
  if (!globalThis.ClipboardItem || !navigator.clipboard?.write)
    throw new CopyFailure(
      "이 브라우저에서 안전한 명령 복사를 사용할 수 없어요. 지원 브라우저와 클립보드 권한을 확인하세요.",
    );
  let issued: ConnectionSnapshot | undefined;
  let stopped = false;
  let preparationFailed = false;
  let pendingSeed: Uint8Array | undefined;
  let cancellation: Promise<void> | undefined;
  const cancelIssued = () => {
    if (!issued) return Promise.resolve();
    return (cancellation ??= gateway.cancel(issued.id));
  };
  const checkActive = () => {
    if (stopped) throw new CopyTimeout();
  };
  const preparation = (async () => {
    try {
      if (previousId) await gateway.cancel(previousId);
      checkActive();
      const { createEnrollmentMaterial, enrollmentCommand } =
        await import("./enrollmentCrypto.ts");
      checkActive();
      const material = createEnrollmentMaterial();
      pendingSeed = material.seed;
      try {
        issued = await gateway.create(
          target,
          material.publicKey,
          crypto.randomUUID(),
        );
        // A late server response must never produce a usable clipboard value.
        // Keep this continuation even after the UI has stopped waiting.
        if (stopped) {
          await cancelIssued();
          throw new CopyTimeout();
        }
        if (gateway.mode === "live") {
          if (!issued.deployment?.releaseVersion)
            throw new Error("배포 정보가 준비되지 않았어요.");
          return new Blob(
            [
              enrollmentCommand(
                issued.deployment.bootstrapUrl,
                issued.id,
                material.seed,
              ),
            ],
            { type: "text/plain" },
          );
        }
        if (gateway.mode !== "preview")
          throw new Error("연결 서비스를 사용할 수 없어요.");
        return new Blob(
          [
            "# Oneport 화면 미리보기입니다. 실제 단말에서 실행할 명령이 없어요.",
          ],
          { type: "text/plain" },
        );
      } finally {
        material.seed.fill(0);
        pendingSeed = undefined;
      }
    } catch (error) {
      preparationFailed = true;
      throw error;
    }
  })();
  void preparation.catch(() => {});
  try {
    const copied = navigator.clipboard.write([
      new ClipboardItem({ "text/plain": preparation }),
    ]);
    await within(Promise.all([preparation, copied]), COPY_TIMEOUT_MS);
    return issued!;
  } catch (error) {
    stopped = true;
    pendingSeed?.fill(0);
    const reason =
      error instanceof CopyTimeout
        ? "명령 복사 완료를 기한 안에 확인하지 못했어요."
        : preparationFailed
          ? "등록 명령을 준비하지 못했어요. 연결 상태와 배포 설정을 확인하세요."
          : "명령을 복사하지 못했어요. 브라우저의 클립보드 권한을 확인하세요.";
    if (issued) {
      try {
        await within(cancelIssued(), CANCEL_TIMEOUT_MS);
      } catch {
        throw new CopyFailure(
          `${reason} 이전 요청 취소도 확인하지 못했어요. 상태를 확인한 뒤 다시 시도하세요.`,
          issued,
        );
      }
      throw new CopyFailure(`${reason} 발급된 미사용 요청은 취소했어요.`);
    }
    // If creation is still pending, preparation cancels its eventual response.
    // A network failure can prevent confirmation; the server's 60s expiry remains.
    throw new CopyFailure(`${reason} 요청 상태를 확인한 뒤 다시 시도하세요.`);
  }
}
