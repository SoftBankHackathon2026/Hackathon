import { useState } from "react";
import { StatusDot } from "../../components/StatusDot";
import type { ConnectionGateway, ConnectionSnapshot } from "./connectionTypes";
import {
  canApproveDevice,
  canApprovePlan,
  codeValid,
  formatTime,
  remainingSeconds,
  SCOPES,
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
            {s.candidate?.os ?? "미확인"} · {s.candidate?.arch}
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
          <dd>
            {s.target.name} ·{" "}
            {s.target.role === "source" ? "출발 환경" : "도착 환경"}
          </dd>
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
                () => gateway.approveDevice(s.id, s.candidate!.digest, code),
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
            <strong>{remainingSeconds(s.approvalExpiresAt, now)}초</strong> ·
            입력 {s.attemptsLeft}회 남음
          </p>
          <label className="connection-check">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
              disabled={!eligible || busy}
            />
            <span>제가 실행한 단말이며, 연결 대상과 범위를 확인했어요.</span>
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

export function PlanReview({
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
  const [context, setContext] = useState(""),
    [confirmed, setConfirmed] = useState(false);
  const plan = s.plan;
  if (!plan)
    return (
      <section>
        <header className="connection-title">
          <h2>연결할 대상을 확인하세요</h2>
          <p>
            터널이 준비됐어요. 실제 계정이나 클러스터를 확인한 다음 변경 계획을
            만들어요.
          </p>
        </header>
        {s.target.kind === "kubernetes" ? (
          <label>
            Kubernetes 문맥
            <select
              value={context}
              onChange={(e) => setContext(e.target.value)}
            >
              <option value="">문맥을 직접 선택하세요</option>
              {s.contexts.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
            <small>
              현재 문맥을 자동으로 선택하지 않아요. kubeconfig 원문은 업로드하지
              않아요.
            </small>
          </label>
        ) : (
          <dl className="connection-facts">
            <div>
              <dt>예상 계정·프로젝트</dt>
              <dd>
                {s.target.expectedIdentity ||
                  "터널 안에서 실제 대상을 확인해요."}
              </dd>
            </div>
            <div>
              <dt>선택한 리전</dt>
              <dd>{s.target.region}</dd>
            </div>
          </dl>
        )}
        <div className="connection-actions">
          <button
            className="primary"
            disabled={
              busy ||
              s.tunnel !== "READY" ||
              (s.target.kind === "kubernetes" && !context)
            }
            onClick={() =>
              run(
                () => gateway.inspect(s.id, context || undefined),
                "대상을 확인했어요. 변경 계획을 검토하세요.",
              )
            }
          >
            대상 확인 후 계획 받기
          </button>
        </div>
      </section>
    );
  return (
    <section>
      <header className="connection-title">
        <h2>변경할 내용을 검토하세요</h2>
        <p>
          승인은 아래 대상과 계획에만 적용돼요. 내용이나 기한이 달라지면 다시
          검토해야 해요.
        </p>
      </header>
      <dl className="connection-facts">
        <div>
          <dt>계획</dt>
          <dd>{plan.title}</dd>
        </div>
        <div>
          <dt>실제 대상</dt>
          <dd>{plan.targetIdentity}</dd>
        </div>
        {s.selectedContext && (
          <div>
            <dt>선택한 문맥</dt>
            <dd>{s.selectedContext}</dd>
          </div>
        )}
        <div>
          <dt>실행 환경</dt>
          <dd>{plan.version}</dd>
        </div>
        <div>
          <dt>승인 기한</dt>
          <dd>{formatTime(plan.expiresAt)} (한국시간)</dd>
        </div>
      </dl>
      <h3>예상 변경</h3>
      <ul className="connection-bullets">
        {plan.changes.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <h3>허용할 권한</h3>
      <ul className="connection-bullets">
        {plan.privileges.map((p) => (
          <li key={p}>
            {SCOPES.find((scope) => scope.value === p)?.label ?? p}
          </li>
        ))}
      </ul>
      <p>재시작: {plan.restart}</p>
      <p>실패 시 처리: {plan.recovery}</p>
      <p className="connection-note">
        사전 점검은 모든 변경을 예측하지 못해요. 호스트 변경과 이관 생성·수정은
        이 계획에 포함되지 않아요.
      </p>
      <details className="connection-details" open>
        <summary>실제 작업 정의와 계획 해시</summary>
        <pre>{plan.definition}</pre>
        <dl className="connection-digests">
          <div>
            <dt>검토할 계획</dt>
            <dd>
              <code>{plan.digest}</code>
            </dd>
          </div>
          {plan.artifacts.map((a) => (
            <div key={a.name}>
              <dt>{a.name}</dt>
              <dd>
                <code>{a.digest}</code>
              </dd>
            </div>
          ))}
        </dl>
      </details>
      <label className="connection-check">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(e) => setConfirmed(e.target.checked)}
          disabled={busy || !canApprovePlan(s, now)}
        />
        <span>
          대상·작업·권한·실패 시 처리를 확인했고, 이 계획의 실행을 승인해요.
        </span>
      </label>
      {!canApprovePlan(s, now) && (
        <p role="status">
          계획 기한·대상·연결 상태 또는 승인 권한을 다시 확인해야 해요.
        </p>
      )}
      <div className="connection-actions">
        <button
          className="primary"
          disabled={busy || !confirmed || !canApprovePlan(s, now)}
          onClick={() =>
            run(
              () => gateway.approvePlan(s.id, plan.digest),
              "검토한 계획을 승인했어요.",
            )
          }
        >
          이 계획 승인
        </button>
        <span>승인하지 않은 작업은 실행하지 않아요.</span>
      </div>
    </section>
  );
}

const jobLabels = {
  waiting: "대기",
  running: "진행 중",
  done: "완료",
  failed: "실패",
  unknown: "미확인",
} as const;
export function ConnectionResult({
  snapshot: s,
}: {
  snapshot: ConnectionSnapshot;
}) {
  const verified =
    s.job === "SUCCEEDED" &&
    s.environmentId &&
    s.verifiedAt &&
    s.tunnel === "READY" &&
    s.device === "APPROVED";
  return (
    <section>
      <header className="connection-title">
        <h2>
          {s.device === "REVOKED"
            ? "연결을 해제했어요"
            : verified
              ? "환경 준비를 마쳤어요"
              : s.tunnel === "REKEYING"
                ? "연결 키를 교체하고 있어요"
                : s.tunnel === "DEGRADED"
                  ? "연결 복구가 필요해요"
                  : s.tunnel === "CONNECTING"
                    ? "보안 연결을 다시 준비하고 있어요"
                    : s.job === "UNKNOWN_RESULT"
                      ? "실제 작업 결과를 확인해야 해요"
                      : s.job === "PARTIAL_FAILURE"
                        ? "일부 작업을 마치지 못했어요"
                        : "승인한 작업을 진행하고 있어요"}
        </h2>
        <p>
          {verified
            ? "승인한 준비 작업의 검증 결과예요. 실제 데이터 이관은 아직 시작하지 않았어요."
            : "확인된 결과와 미확인 단계를 나눠 표시해요."}
        </p>
      </header>
      {s.jobs.length > 0 && (
        <ol className="connection-check-list">
          {s.jobs.map((j) => (
            <li key={j.name}>
              <div>
                <StatusDot
                  tone={
                    j.state === "done"
                      ? "ok"
                      : j.state === "failed"
                        ? "bad"
                        : j.state === "running"
                          ? "running"
                          : "idle"
                  }
                >
                  {j.name}
                </StatusDot>
                <small>{j.detail}</small>
              </div>
              <span>{jobLabels[j.state]}</span>
            </li>
          ))}
        </ol>
      )}
      {s.cleanup && (
        <>
          <h3>해제와 정리 결과</h3>
          <dl className="connection-facts">
            {(
              [
                ["서버 권한 철회", s.cleanup.server],
                ["터널 차단", s.cleanup.gateway],
                ["고객 권한 정리", s.cleanup.resources],
                ["단말 파일 정리", s.cleanup.local],
              ] as const
            ).map(([name, done]) => (
              <div key={name}>
                <dt>{name}</dt>
                <dd>
                  <StatusDot tone={done ? "ok" : "idle"}>
                    {done ? "확인 완료" : "미확인"}
                  </StatusDot>
                </dd>
              </div>
            ))}
          </dl>
          <p className="connection-note">
            접속할 수 없는 단말의 파일이나 고객 자원이 정리되었다고 보장할 수
            없어요.
          </p>
        </>
      )}
      <dl className="connection-facts">
        <div>
          <dt>환경 이름</dt>
          <dd>{s.target.name}</dd>
        </div>
        <div>
          <dt>대상</dt>
          <dd>{s.targetIdentity ?? "아직 확인되지 않았어요."}</dd>
        </div>
        <div>
          <dt>마지막 확인</dt>
          <dd>{formatTime(s.observedAt)} (한국시간)</dd>
        </div>
        {s.device === "APPROVED" && (
          <>
            <div>
              <dt>연결 키 세대</dt>
              <dd>{s.epoch}세대</dd>
            </div>
            <div>
              <dt>연결 키 만료</dt>
              <dd>{formatTime(s.keyExpiresAt)} (한국시간)</dd>
            </div>
          </>
        )}
      </dl>
      {verified && (
        <div className="connection-next">
          <button className="primary" disabled>
            다음: 앱 선택
          </button>
          <p>
            앱 선택은 준비 중이에요. 환경 준비 완료와 이관 완료는 별도 상태예요.
          </p>
        </div>
      )}
    </section>
  );
}
