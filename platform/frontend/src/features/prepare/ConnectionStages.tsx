import { useState } from "react";
import { StatusDot } from "../../components/StatusDot";
import type { ConnectionGateway, ConnectionSnapshot } from "./connectionTypes";
import {
  canApproveDevice,
  codeValid,
  normalizeCode,
  remainingSeconds,
  TUNNEL_CHECKS,
} from "./model";

type Run = (action: () => Promise<void>, message?: string) => void;
export function DeviceApproval({
  snapshot: s,
  gateway,
  now,
  busy,
  run,
}: {
  snapshot: ConnectionSnapshot;
  gateway: ConnectionGateway;
  now: number;
  busy: boolean;
  run: Run;
}) {
  const [code, setCode] = useState(""),
    [confirmed, setConfirmed] = useState(false);
  const eligible = canApproveDevice(s, now);
  return (
    <section>
      <header className="connection-title">
        <h2>실행한 단말을 확인하세요</h2>
        <p>
          터미널에 표시된 8자리 확인 코드를 입력하세요. 승인하면 보안 연결
          준비만 허용돼요.
        </p>
      </header>
      <dl className="connection-facts">
        <div>
          <dt>보고된 단말 이름</dt>
          <dd>{s.candidate?.hostname ?? "미확인"}</dd>
        </div>
        <div>
          <dt>운영체제</dt>
          <dd>
            {s.candidate?.os ?? "미확인"}, {s.candidate?.arch}
          </dd>
        </div>
        <div>
          <dt>공개키 지문</dt>
          <dd>
            <code>{s.candidate?.fingerprint ?? "미확인"}</code>
          </dd>
        </div>
        <div>
          <dt>요청 대상</dt>
          <dd>환경정보 수집용 단말</dd>
        </div>
      </dl>
      <p className="connection-note">
        단말 이름과 운영체제는 단말이 보고한 참고 정보예요. 확인 코드와 공개키
        증명으로 이 요청을 확인해요.
      </p>
      {s.device === "REJECTED" ? (
        <p className="connection-notice">
          {s.issue ?? "승인 대기가 종료됐어요. 새 등록을 시작하세요."}
        </p>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (eligible && confirmed && codeValid(code))
              run(
                () =>
                  gateway.approveDevice(
                    s.id,
                    s.candidate!.digest,
                    normalizeCode(code),
                  ),
                "단말을 승인했어요. 보안 연결을 준비해요.",
              );
          }}
        >
          <label className="connection-code-label">
            터미널 확인 코드
            <input
              className="connection-code"
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="XXXX XXXX"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              maxLength={12}
              aria-describedby="connection-code-help"
              disabled={!eligible || busy}
            />
          </label>
          <p id="connection-code-help">
            승인까지{" "}
            <strong>{remainingSeconds(s.approvalExpiresAt, now)}초</strong>,
            입력 {s.attemptsLeft}회 남음
          </p>
          <label className="connection-check">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
              disabled={!eligible || busy}
            />
            <span>제가 실행한 단말이며, 환경정보 수집을 승인해요.</span>
          </label>
          {!s.permissions.approveDevice && (
            <p>이 단말을 승인할 권한이 없어요. 조직 관리자에게 문의하세요.</p>
          )}
          {remainingSeconds(s.approvalExpiresAt, now) === 0 && (
            <p role="status">승인 기한이 끝났어요. 새 등록을 시작하세요.</p>
          )}
          <div className="connection-actions">
            <button
              type="submit"
              className="primary"
              disabled={!eligible || !confirmed || !codeValid(code) || busy}
            >
              단말 연결 승인
            </button>
            <button
              type="button"
              className="secondary"
              disabled={!eligible || busy}
              onClick={() =>
                run(() => gateway.reject(s.id), "단말을 거절했어요.")
              }
            >
              이 단말 거절
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

export function TunnelProgress({
  snapshot: s,
}: {
  snapshot: ConnectionSnapshot;
}) {
  return (
    <section>
      <header className="connection-title">
        <h2>
          {s.tunnel === "DEGRADED"
            ? "보안 연결을 확인하지 못했어요"
            : "보안 연결을 준비하고 있어요"}
        </h2>
        <p>승인된 단말과 암호화 통로를 만들고 양방향 통신을 확인해요.</p>
      </header>
      <ol className="connection-check-list">
        {TUNNEL_CHECKS.map((text, i) => (
          <li key={text}>
            <StatusDot
              tone={
                s.tunnelChecks > i
                  ? "ok"
                  : s.tunnel === "DEGRADED"
                    ? "idle"
                    : s.tunnelChecks === i
                      ? "running"
                      : "idle"
              }
            >
              {text}
            </StatusDot>
            <span>
              {s.tunnelChecks > i
                ? "확인 완료"
                : s.tunnelChecks === i && s.tunnel !== "DEGRADED"
                  ? "확인 중"
                  : "대기"}
            </span>
          </li>
        ))}
      </ol>
      <p className="connection-note">
        모든 확인이 끝나기 전에는 고객 환경의 설정이나 설치 작업을 시작하지
        않아요.
      </p>
    </section>
  );
}
