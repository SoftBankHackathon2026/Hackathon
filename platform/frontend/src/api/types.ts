export type AuthMode = "operator" | "demo" | "login";
export type Role = "owner" | "member" | "viewer";
export type Account = { email: string; team: string; role: Role };
export type Member = { email: string; role: Role; created_at: string };
export type Access = {
  auth_mode: AuthMode;
  execution_configured: boolean;
  user: Account | null;
};
