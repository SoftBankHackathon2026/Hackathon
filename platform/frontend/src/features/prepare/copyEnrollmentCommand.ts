import type {
  ConnectionGateway,
  ConnectionSnapshot,
  ConnectionTarget,
} from "./connectionTypes.ts";

export class CopyFailure extends Error {
  request?: ConnectionSnapshot;
  constructor(message: string, request?: ConnectionSnapshot) {
    super(message);
    this.request = request;
  }
}

// Start the clipboard operation synchronously within the click. Its Blob is
// resolved later, preserving Safari's user activation across asynchronous work.
export async function copyEnrollmentCommand(
  gateway: ConnectionGateway,
  target: ConnectionTarget,
  previousId?: string,
) {
  if (!gateway.available)
    throw new CopyFailure(gateway.reason ?? "연결 서비스를 사용할 수 없어요.");
  if (!globalThis.ClipboardItem || !navigator.clipboard?.write)
    throw new CopyFailure(
      "이 브라우저에서 안전한 명령 복사를 사용할 수 없어요. 지원 브라우저와 클립보드 권한을 확인하세요.",
    );
  let issued: ConnectionSnapshot | undefined;
  const preparation = (async () => {
    if (previousId) await gateway.cancel(previousId);
    const { createEnrollmentMaterial } = await import("./enrollmentCrypto.ts");
    const material = createEnrollmentMaterial();
    try {
      issued = await gateway.create(
        target,
        material.publicKey,
        crypto.randomUUID(),
      );
      // No live publication until the release and cancellation contracts exist.
      // Preview clipboard contents are inert and never contain a seed.
      if (gateway.mode !== "preview")
        throw new Error("배포 주소가 확정되지 않았어요.");
      return new Blob(
        ["# Oneport 화면 미리보기입니다. 실제 단말에서 실행할 명령이 없어요."],
        { type: "text/plain" },
      );
    } finally {
      material.seed.fill(0);
    }
  })();
  void preparation.catch(() => {});
  try {
    await navigator.clipboard.write([
      new ClipboardItem({ "text/plain": preparation }),
    ]);
    await preparation;
    return issued!;
  } catch {
    try {
      await preparation;
    } catch {
      /* Never expose raw payloads in errors. */
    }
    if (issued) {
      try {
        await gateway.cancel(issued.id);
      } catch {
        throw new CopyFailure(
          "복사하지 못했어요. 이전 요청 취소도 확인하지 못했어요. 상태를 확인한 뒤 다시 시도하세요.",
          issued,
        );
      }
    }
    throw new CopyFailure(
      issued
        ? "명령을 복사하지 못했어요. 발급된 미사용 요청은 취소했어요. 클립보드 권한을 확인하고 다시 시도하세요."
        : "등록·복사를 완료하지 못했어요. 요청 상태와 클립보드 권한을 확인한 뒤 다시 시도하세요.",
    );
  }
}
