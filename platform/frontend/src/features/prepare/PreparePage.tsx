import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { PageHeader } from "../../components/PageKit";
import { StatusDot } from "../../components/StatusDot";
import { Icon } from "../../components/Icon";
import { unavailableConnections } from "../../api/connections";
import type {
  ConnectionGateway,
  ConnectionSnapshot,
  ConnectionTarget,
} from "./connectionTypes";
import { ConnectionTargetForm } from "./ConnectionTargetForm";
import {
  ConnectionResult,
  DeviceApproval,
  PlanReview,
  TunnelProgress,
} from "./ConnectionStages";
import { CopyFailure, copyEnrollmentCommand } from "./copyEnrollmentCommand";
import {
  connectionStatus,
  emptyTarget,
  formatTime,
  PROVIDERS,
  remainingSeconds,
  STEPS,
  stepFor,
  targetValid,
} from "./model";
import "./prepare.css";

function useServerClock(snapshot: ConnectionSnapshot | null) {
  const [now, setNow] = useState(snapshot?.serverNow ?? Date.now());
  useEffect(() => {
    const origin = performance.now(),
      server = snapshot?.serverNow ?? Date.now();
    const update = () => setNow(server + performance.now() - origin);
    update();
    const timer = setInterval(update, 500);
    const visible = () => {
      if (!document.hidden) update();
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [snapshot?.serverNow, snapshot?.id]);
  return now;
}

export function PreparePage({
  onNavigate,
  requestId,
  team = "",
  gateway = unavailableConnections,
  previewTools,
}: {
  onNavigate: (hash: string) => void;
  requestId?: string;
  team?: string;
  gateway?: ConnectionGateway;
  previewTools?: (
    snapshot: ConnectionSnapshot | null,
    target: ConnectionTarget,
  ) => ReactNode;
}) {
  const snapshots = useSyncExternalStore(gateway.subscribe, gateway.list);
  const snapshot = snapshots.find((s) => s.id === requestId) ?? null;
  const [target, setTarget] = useState<ConnectionTarget>(() =>
    emptyTarget(team),
  );
  const [ready, setReady] = useState(false),
    [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [error, setError] = useState("");
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const lock = useRef(false),
    alive = useRef(true),
    heading = useRef<HTMLDivElement>(null);
  const now = useServerClock(snapshot);
  const current = snapshot?.target ?? target;
  const step = stepFor(snapshot, ready);
  const stale = Boolean(snapshot && now - snapshot.observedAt > 20_000);
  const busyId = useRef(requestId);
  const copiedId = useRef<string | undefined>(undefined);
  const previousStep = useRef(step);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    busyId.current = requestId;
    setError("");
    if (copiedId.current !== requestId) setMessage("");
    setConfirmRevoke(false);
  }, [requestId]);
  useEffect(() => {
    if (previousStep.current !== step)
      heading.current?.focus({ preventScroll: true });
    previousStep.current = step;
  }, [step]);
  const run = (action: () => Promise<void>, success = "") => {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    const startedId = requestId;
    void action()
      .then(() => {
        if (alive.current && busyId.current === startedId) setMessage(success);
      })
      .catch((e) => {
        if (alive.current && busyId.current === startedId)
          setError(
            e instanceof Error
              ? e.message
              : "요청을 처리하지 못했어요. 상태를 다시 확인하세요.",
          );
      })
      .finally(() => {
        lock.current = false;
        if (alive.current) setBusy(false);
      });
  };
  function startNew() {
    if (busy) return;
    setTarget(emptyTarget(team));
    setReady(false);
    setAcknowledged(false);
    setError("");
    setMessage("");
    onNavigate("#prepare");
  }
  function copy() {
    if (lock.current || !acknowledged || !gateway.available || stale) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    const startedId = requestId;
    // The write call begins here, before the first await.
    void copyEnrollmentCommand(gateway, current, snapshot?.id)
      .then((s) => {
        if (!alive.current || busyId.current !== startedId) return;
        copiedId.current = s.id;
        onNavigate("#prepare/" + encodeURIComponent(s.id));
        setMessage(
          gateway.mode === "preview"
            ? "미리보기 안내를 복사했어요. 실제 실행 명령은 복사하지 않았어요."
            : "명령을 복사했어요.",
        );
      })
      .catch((e) => {
        if (!alive.current || busyId.current !== startedId) return;
        if (e instanceof CopyFailure && e.request)
          onNavigate("#prepare/" + encodeURIComponent(e.request.id));
        setError(
          e instanceof Error
            ? e.message
            : "복사하지 못했어요. 다시 시도하세요.",
        );
      })
      .finally(() => {
        lock.current = false;
        if (alive.current) setBusy(false);
      });
  }
  const status = snapshot ? connectionStatus(snapshot) : null;
  return (
    <div className="pk-page connection-page">
      <PageHeader
        title="마이그레이션"
        tabs={[
          { id: "overview", label: "개요" },
          { id: "prepare", label: "준비" },
        ]}
        active="prepare"
        onTab={(id) => id === "overview" && onNavigate("#migrations")}
      />
      {gateway.mode === "preview" && (
        <div className="connection-preview-banner">
          <Icon name="code" size={18} />
          <span>
            <strong>화면 미리보기</strong> 실제 환경에 연결하지 않아요. 아래
            동작과 결과는 로컬 예시예요.
          </span>
        </div>
      )}
      <ol className="connection-phases" aria-label="마이그레이션 준비 단계">
        <li aria-current="step">
          <span>1</span>환경 연결
        </li>
        <li>
          <span>2</span>앱 선택 <small>준비 중</small>
        </li>
      </ol>
      {!gateway.available && (
        <p className="connection-notice" role="status">
          {gateway.reason}
        </p>
      )}
      <div className="connection-layout" aria-busy={busy}>
        <aside className="connection-sidebar" aria-label="연결 환경">
          <div className="connection-sidebar-heading">
            <h2>연결 환경</h2>
            <span>{snapshots.length}개</span>
          </div>
          <button
            className="secondary connection-add"
            onClick={startNew}
            disabled={busy}
          >
            <Icon name="layers" size={16} />
            환경 연결 추가
          </button>
          {snapshots.length === 0 ? (
            <p className="connection-empty">
              연결한 환경이 없어요.
              <br />
              출발 환경부터 추가해 보세요.
            </p>
          ) : (
            <ul className="connection-environments">
              {snapshots.map((s) => {
                const state = connectionStatus(s);
                return (
                  <li key={s.id}>
                    <button
                      disabled={busy}
                      aria-current={s.id === requestId ? "true" : undefined}
                      onClick={() =>
                        onNavigate("#prepare/" + encodeURIComponent(s.id))
                      }
                    >
                      <strong>{s.target.name}</strong>
                      <span>
                        {
                          PROVIDERS.find((p) => p.value === s.target.kind)
                            ?.title
                        }{" "}
                        · {s.target.role === "source" ? "출발" : "도착"}
                      </span>
                      <StatusDot tone={state.tone}>{state.label}</StatusDot>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          <div className="connection-sidebar-note">
            <Icon name="shield" size={18} />
            <h3>연결과 실행은 별도 승인</h3>
            <p>
              단말 확인 후 보안 연결을 만들어요. 설치와 권한 변경은 내용을
              검토한 뒤 승인해요.
            </p>
          </div>
        </aside>
        <div className="connection-main">
          <div className="connection-main-heading">
            <div>
              <h2>{snapshot ? current.name : "새 환경 연결"}</h2>
              {snapshot && (
                <p>
                  {current.organization} / {current.project}
                </p>
              )}
            </div>
            {status && (
              <StatusDot tone={stale ? "idle" : status.tone}>
                {stale ? "현재 상태 미확인" : status.label}
              </StatusDot>
            )}
          </div>
          <ol className="connection-progress" aria-label="환경 연결 진행">
            {STEPS.map((label, i) => (
              <li
                key={label}
                className={i < step ? "done" : ""}
                aria-current={i === step ? "step" : undefined}
              >
                <span>{i + 1}</span>
                {label}
              </li>
            ))}
          </ol>
          <div ref={heading} tabIndex={-1} className="connection-work">
            <div className="connection-feedback" aria-live="polite">
              {message && <p className="connection-success">{message}</p>}
            </div>
            {error && (
              <p className="connection-error" role="alert">
                {error}
              </p>
            )}
            {stale && (
              <p className="connection-notice" role="status">
                상태 갱신이 끊겼어요. 마지막 확인{" "}
                {formatTime(snapshot?.observedAt)}. 연결이 다시 확인될 때까지
                승인과 실행을 멈춰요.
              </p>
            )}
            {requestId && !snapshot ? (
              <div className="connection-title">
                <h2>요청을 찾지 못했어요</h2>
                <p>
                  다른 조직의 요청이거나 종료된 요청일 수 있어요. 환경 목록에서
                  다시 선택하세요.
                </p>
                <button className="secondary" onClick={startNew}>
                  새 환경 구성
                </button>
              </div>
            ) : (
              <>
                {snapshot?.blockedUntil && (
                  <p className="connection-error" role="alert">
                    {snapshot.issue}
                    <br />
                    차단 종료:{" "}
                    {new Intl.DateTimeFormat("ko-KR", {
                      timeZone: "Asia/Seoul",
                      dateStyle: "medium",
                      timeStyle: "short",
                    }).format(snapshot.blockedUntil)}{" "}
                    (한국시간)
                  </p>
                )}
                {snapshot?.issue &&
                  !snapshot.blockedUntil &&
                  snapshot.device !== "REJECTED" && (
                    <p className="connection-notice">{snapshot.issue}</p>
                  )}
                {step === 0 && (
                  <ConnectionTargetForm
                    value={target}
                    onChange={setTarget}
                    onContinue={() => {
                      if (targetValid(target)) {
                        setReady(true);
                        setAcknowledged(false);
                      }
                    }}
                  />
                )}
                {step === 1 && (
                  <section>
                    <header className="connection-title">
                      <h2>
                        {snapshot?.registration === "EXPIRED"
                          ? "등록 기한이 끝났어요"
                          : "단말에서 명령 한 줄을 실행하세요"}
                      </h2>
                      <p>
                        명령을 복사하는 순간 등록 자격이 만들어져요. 60초 안에
                        등록 증명이 접수되어야 해요.
                      </p>
                    </header>
                    <div className="connection-readiness">
                      <h3>실행할 단말 준비</h3>
                      <dl>
                        <div>
                          <dt>운영체제</dt>
                          <dd>Ubuntu 24.04 LTS · amd64 또는 arm64</dd>
                        </div>
                        <div>
                          <dt>도구</dt>
                          <dd>
                            Bash · curl · systemd ·{" "}
                            {current.kind === "kubernetes"
                              ? "kubectl"
                              : current.kind === "aws"
                                ? "aws"
                                : "gcloud"}
                          </dd>
                        </div>
                        <div>
                          <dt>관리 권한</dt>
                          <dd>
                            사전 설치된 보조 프로그램 또는 비대화형 관리자 권한
                          </dd>
                        </div>
                        <div>
                          <dt>외부 통신</dt>
                          <dd>TCP 443·8443, UDP 51820</dd>
                        </div>
                      </dl>
                      <p>
                        준비 안내예요. 실제 도구·권한·시계·통신 검사는 단말에서
                        진행해요.
                      </p>
                    </div>
                    <label className="connection-check">
                      <input
                        type="checkbox"
                        checked={acknowledged}
                        onChange={(e) => setAcknowledged(e.target.checked)}
                        disabled={busy}
                      />
                      <span>
                        이 환경에 접근할 수 있는 지원 단말을 준비했어요.
                      </span>
                    </label>
                    <div className="connection-command">
                      <div>
                        <h3>실행 명령</h3>
                        <span>등록 비밀은 화면에 표시하지 않아요.</span>
                      </div>
                      <pre aria-label="등록 명령 형식">
                        {gateway.mode === "preview"
                          ? "# 미리보기용 안내만 복사돼요. 실제 실행 명령이 없어요."
                          : "curl -qfsS --proto '=https' <배포 주소>/i | MIGRATION_ENROLL='<요청 ID>.••••••••' bash"}
                      </pre>
                    </div>
                    <div className="connection-actions">
                      <button
                        className="primary"
                        disabled={
                          !gateway.available || !acknowledged || busy || stale
                        }
                        onClick={copy}
                      >
                        <Icon name="code" size={16} />
                        {busy
                          ? "복사 준비 중…"
                          : snapshot
                            ? "새 명령 복사"
                            : gateway.mode === "preview"
                              ? "미리보기 명령 복사"
                              : "명령 복사"}
                      </button>
                      {snapshot && (
                        <span role="timer" aria-label="등록까지 남은 시간">
                          등록 접수까지{" "}
                          <strong>
                            {remainingSeconds(
                              snapshot.registrationExpiresAt,
                              now,
                            )}
                            초
                          </strong>
                        </span>
                      )}
                      {!snapshot && (
                        <button
                          className="secondary"
                          disabled={busy}
                          onClick={() => setReady(false)}
                        >
                          환경 설정 수정
                        </button>
                      )}
                    </div>
                    <p className="connection-note">
                      60초는 설치 완료 기한이 아니에요. 등록 후에는 최대 5분
                      안에 웹에서 단말을 확인해요. 셸 기록과 클립보드에는 복사
                      내용이 남을 수 있어요.
                    </p>
                  </section>
                )}
                {snapshot && (
                  <fieldset
                    className="connection-stage-content"
                    disabled={busy || stale}
                  >
                    {step === 2 && (
                      <DeviceApproval
                        key={snapshot.id}
                        snapshot={snapshot}
                        gateway={gateway}
                        now={now}
                        busy={busy || stale}
                        run={run}
                      />
                    )}
                    {step === 3 && <TunnelProgress snapshot={snapshot} />}
                    {step === 4 && (
                      <PlanReview
                        key={
                          snapshot.id + ":" + (snapshot.plan?.digest ?? "none")
                        }
                        snapshot={snapshot}
                        gateway={gateway}
                        now={now}
                        busy={busy || stale}
                        run={run}
                      />
                    )}
                    {step === 5 && <ConnectionResult snapshot={snapshot} />}
                  </fieldset>
                )}
                {snapshot?.device === "APPROVED" && (
                  <section className="connection-management">
                    <h3>연결 관리</h3>
                    <p>
                      마지막 확인 {formatTime(snapshot.observedAt)} · 키{" "}
                      {snapshot.epoch}세대
                      {snapshot.keyExpiresAt
                        ? " · 만료 " + formatTime(snapshot.keyExpiresAt)
                        : ""}
                    </p>
                    <div className="connection-actions">
                      {snapshot.tunnel === "DEGRADED" && (
                        <button
                          className="primary"
                          disabled={busy || stale}
                          onClick={() =>
                            run(
                              () => gateway.reconnect(snapshot.id),
                              "새 보안 연결을 준비해요.",
                            )
                          }
                        >
                          새 연결로 재접속
                        </button>
                      )}
                      {!confirmRevoke ? (
                        <button
                          className="secondary"
                          disabled={busy || !snapshot.permissions.revoke}
                          onClick={() => setConfirmRevoke(true)}
                        >
                          연결 해제
                        </button>
                      ) : (
                        <div className="connection-revoke">
                          <p>
                            새 작업과 터널을 차단할까요? 고객 권한과 단말 파일
                            정리 결과는 별도로 확인해요.
                          </p>
                          <button
                            className="connection-danger"
                            disabled={busy}
                            onClick={() =>
                              run(
                                () => gateway.revoke(snapshot.id),
                                "연결을 해제했어요. 정리 결과를 확인하세요.",
                              )
                            }
                          >
                            연결 해제 확인
                          </button>
                          <button
                            className="secondary"
                            onClick={() => setConfirmRevoke(false)}
                          >
                            취소
                          </button>
                        </div>
                      )}
                    </div>
                  </section>
                )}
                {snapshot &&
                  (snapshot.registration === "EXPIRED" ||
                    snapshot.device === "REJECTED" ||
                    snapshot.device === "REVOKED") && (
                    <div className="connection-actions">
                      <button
                        className="secondary"
                        disabled={busy}
                        onClick={() => {
                          setTarget(snapshot.target);
                          setReady(true);
                          setAcknowledged(false);
                          onNavigate("#prepare");
                        }}
                      >
                        이 환경으로 다시 시작
                      </button>
                    </div>
                  )}
                {snapshot && (
                  <details className="connection-details connection-history">
                    <summary>
                      연결 기록 <span>{snapshot.events.length}건</span>
                    </summary>
                    <ol>
                      {snapshot.events
                        .slice()
                        .reverse()
                        .map((e, i) => (
                          <li key={i}>
                            <time>{formatTime(e.at)}</time>
                            <span>{e.message}</span>
                          </li>
                        ))}
                    </ol>
                  </details>
                )}
              </>
            )}
          </div>
          {previewTools?.(snapshot, current)}
        </div>
      </div>
    </div>
  );
}
