export const UNAUTHORIZED_EVENT = "oneport:unauthorized";

export type RequestOptions = {
  token?: string;
  method?: "GET" | "POST";
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
};

async function readJson<T>(response: Response): Promise<T> {
  if (response.status === 401)
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  let body: unknown = null;
  if (response.status !== 204) {
    try {
      body = await response.json();
    } catch {
      throw new Error(
        response.status >= 500
          ? `플랫폼 서버에 잠시 연결할 수 없어요. HTTP ${response.status} 응답이에요. 잠시 후 상태를 다시 확인하세요.`
          : "서버 응답을 읽지 못했어요. 잠시 후 다시 시도하세요.",
      );
    }
  }
  const detail =
    body && typeof body === "object" && "detail" in body
      ? body.detail
      : undefined;
  if (!response.ok) {
    throw new Error(
      typeof detail === "string"
        ? detail
        : "요청을 처리하지 못했어요. 잠시 후 다시 시도하세요.",
    );
  }
  return body as T;
}

export const OFFLINE_MESSAGE =
  "플랫폼 서버에 연결하지 못했어요. 네트워크를 확인하고 다시 불러오세요.";

export async function request<T>(
  path: string,
  { token = "", method, body, headers, signal }: RequestOptions = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1/${path}`, {
      method,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...headers,
      },
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new Error(OFFLINE_MESSAGE);
  }
  return readJson<T>(response);
}

export function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

export function environmentPath(environmentId: string, path: string) {
  return `environments/${encodeURIComponent(environmentId)}/${path}`;
}
