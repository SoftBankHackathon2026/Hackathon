import { useState } from "react";
import { Icon } from "../../components/Icon";
import { errorMessage } from "../../api/client";
import type { Account } from "../../api/types";
import { useAuthenticate } from "./queries";

export function LoginPage({
  onSignedIn,
}: {
  onSignedIn: (account: Account) => void;
}) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [team, setTeam] = useState("");
  const [error, setError] = useState("");
  const signup = mode === "signup";
  const authenticate = useAuthenticate(signup);
  const busy = authenticate.isPending;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    try {
      onSignedIn(await authenticate.mutateAsync({ email, password, team }));
    } catch (e) {
      setError(errorMessage(e, "로그인하지 못했어요."));
    }
  }
  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <div className="login-brand">
          <span className="login-mark">
            <Icon name="rocket" size={20} />
          </span>
          <strong>oneport</strong>
        </div>
        <h1>{signup ? "계정 만들기" : "로그인"}</h1>
        <p className="notice-line">
          {signup
            ? "같은 팀 이름으로 가입한 사람끼리 마이그레이션 현황과 보안 상태를 함께 봐요."
            : "로그인하면 우리 팀의 마이그레이션 현황과 보안 상태를 볼 수 있어요."}
        </p>
        {error && (
          <p className="banner" role="alert">
            {error}
          </p>
        )}
        <label>
          이메일
          <input
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label>
          비밀번호
          <input
            type="password"
            autoComplete={signup ? "new-password" : "current-password"}
            required
            minLength={8}
            maxLength={128}
            placeholder={signup ? "8자 이상" : undefined}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {signup && (
          <label>
            부서(팀)
            <input
              required
              maxLength={40}
              placeholder="예: platform"
              value={team}
              onChange={(e) => setTeam(e.target.value)}
            />
          </label>
        )}
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "확인하는 중…" : signup ? "가입하고 시작하기" : "로그인"}
        </button>
        <button
          type="button"
          className="text"
          onClick={() => {
            setMode(signup ? "login" : "signup");
            setError("");
          }}
        >
          {signup ? "이미 계정이 있다면 로그인" : "처음이신가요? 계정 만들기"}
        </button>
      </form>
    </div>
  );
}
