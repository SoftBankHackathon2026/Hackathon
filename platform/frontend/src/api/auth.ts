import { request } from "./client";
import type { Access, Account } from "./types";

export function getAccess(signal?: AbortSignal) {
  return request<Access>("access", { signal });
}

export function signUp(email: string, password: string, team: string) {
  return request<Account>("auth/signup", {
    method: "POST",
    body: { email, password, team },
  });
}

export function logIn(email: string, password: string) {
  return request<Account>("auth/login", {
    method: "POST",
    body: { email, password },
  });
}

export function logOut() {
  return request<null>("auth/logout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
}
