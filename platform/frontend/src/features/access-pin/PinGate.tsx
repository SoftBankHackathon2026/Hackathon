import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { UNAUTHORIZED_EVENT } from "../../api/client";
import { Icon } from "../../components/Icon";
import { PIN_LENGTH, pinDigits, readPinStatus, submitPin } from "./pin";
import "./pin.css";

type State = "checking" | "open" | "locked";

export function PinGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>("checking");
  const check = useCallback(() => {
    readPinStatus()
      .then((s) => setState(!s.required || s.unlocked ? "open" : "locked"))
      .catch(() => setState("open"));
  }, []);
  useEffect(() => {
    check();
    window.addEventListener(UNAUTHORIZED_EVENT, check);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, check);
  }, [check]);
  if (state === "checking") return <div className="login" />;
  if (state === "locked")
    return <PinPage onUnlocked={() => setState("open")} />;
  return children;
}

function PinPage({ onUnlocked }: { onUnlocked: () => void }) {
  const [pin, setPin] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  async function unlock(value: string) {
    if (busy || value.length !== PIN_LENGTH) return;
    setBusy(true);
    const failure = await submitPin(value);
    setBusy(false);
    if (failure === null) return onUnlocked();
    setError(failure);
    setPin("");
    input.current?.focus();
  }
  return (
    <div className="login">
      <form
        className="card login-card pin-card"
        onSubmit={(event) => {
          event.preventDefault();
          void unlock(pin);
        }}
      >
        <div className="login-brand">
          <span className="login-mark">
            <Icon name="rocket" size={20} />
          </span>
          <strong>oneport</strong>
        </div>
        <h1>접속 비밀번호</h1>
        <p className="notice-line">
          공유받은 숫자 {PIN_LENGTH}자리를 입력하면 플랫폼을 볼 수 있어요.
        </p>
        {error && (
          <p className="banner" role="alert">
            {error}
          </p>
        )}
        <label className="pin-field">
          <span className="sr-only">비밀번호 {PIN_LENGTH}자리</span>
          <input
            ref={input}
            type="password"
            inputMode="numeric"
            autoComplete="one-time-code"
            autoFocus
            maxLength={PIN_LENGTH}
            value={pin}
            disabled={busy}
            onChange={(e) => {
              const next = pinDigits(e.target.value);
              setPin(next);
              setError("");
              if (next.length === PIN_LENGTH) void unlock(next);
            }}
          />
          <span className="pin-cells" aria-hidden="true">
            {Array.from({ length: PIN_LENGTH }, (_, i) => (
              <span
                key={i}
                className={`pin-cell${i < pin.length ? " filled" : ""}${i === pin.length ? " current" : ""}`}
              />
            ))}
          </span>
        </label>
        <button
          type="submit"
          className="primary"
          disabled={busy || pin.length !== PIN_LENGTH}
        >
          {busy ? "확인하는 중…" : "들어가기"}
        </button>
      </form>
    </div>
  );
}
