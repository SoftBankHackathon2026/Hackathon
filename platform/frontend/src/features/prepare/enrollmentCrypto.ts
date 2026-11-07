import { ml_dsa65 } from "@noble/post-quantum/ml-dsa.js";

export function base64url(bytes: Uint8Array) {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}
export function createEnrollmentMaterial() {
  const seed = crypto.getRandomValues(new Uint8Array(32));
  try {
    const keys = ml_dsa65.keygen(seed);
    try {
      return { seed, publicKey: base64url(keys.publicKey) };
    } finally {
      keys.secretKey.fill(0);
    }
  } catch {
    seed.fill(0);
    throw new Error("등록 키를 만들지 못했어요. 다시 시도하세요.");
  }
}
export function enrollmentCommand(
  url: string,
  requestId: string,
  seed: Uint8Array,
) {
  const parsed = new URL(url);
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    url
      .split("/")
      .slice(3)
      .some((segment) => segment === "." || segment === "..") ||
    !/^https:\/\/[A-Za-z0-9.-]+(?::[0-9]+)?\/[A-Za-z0-9/_.-]+$/.test(url) ||
    !/^[A-Za-z0-9_-]{22}$/.test(requestId) ||
    seed.length !== 32
  ) {
    throw new Error("등록 명령 설정이 올바르지 않아요.");
  }
  const command = `curl -qfsS --proto '=https' ${url} | MIGRATION_ENROLL='${requestId}.${base64url(seed)}' bash`;
  if (command.length > 256)
    throw new Error("등록 명령이 허용 길이를 넘었어요.");
  return command;
}
