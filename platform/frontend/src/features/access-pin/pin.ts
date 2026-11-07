export const PIN_LENGTH = 4;

export function pinDigits(value: string) {
  return value.replace(/\D/g, "").slice(0, PIN_LENGTH);
}

export type PinStatus = { required: boolean; unlocked: boolean };

export async function readPinStatus(): Promise<PinStatus> {
  const response = await fetch("/api/v1/access-pin");
  if (!response.ok) throw new Error(String(response.status));
  return (await response.json()) as PinStatus;
}

export const PIN_RETRY_MESSAGE = "확인하지 못했어요. 잠시 후 다시 시도하세요.";

export async function submitPin(pin: string): Promise<string | null> {
  let response: Response;
  try {
    response = await fetch("/api/v1/access-pin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin }),
    });
  } catch {
    return PIN_RETRY_MESSAGE;
  }
  if (response.ok) return null;
  const body = (await response.json().catch(() => ({}))) as {
    detail?: unknown;
  };
  return typeof body.detail === "string" ? body.detail : PIN_RETRY_MESSAGE;
}
