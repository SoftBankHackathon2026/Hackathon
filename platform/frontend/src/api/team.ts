import { request } from "./client";
import type { Member, Role } from "./types";

export function getTeam(signal?: AbortSignal) {
  return request<{ team: string; members: Member[] }>("team", { signal });
}

export function setRole(email: string, role: Role) {
  return request<Member>("team/role", {
    method: "POST",
    body: { email, role },
  });
}
