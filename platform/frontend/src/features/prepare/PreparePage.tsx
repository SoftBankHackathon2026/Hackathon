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
  CollectionContext,
  ConnectionGateway,
  ConnectionSnapshot,
} from "./connectionTypes";
import { ConnectionTargetForm } from "./ConnectionTargetForm";
import { DeviceApproval, TunnelProgress } from "./ConnectionStages";
import { CollectionStart } from "./CollectionStart";
import {
  CleanupResult,
  CollectionSource,
  RegisteredEnvironment,
} from "./ConnectionRegistration";
import { CopyFailure, copyEnrollmentCommand } from "./copyEnrollmentCommand";
import { connectionStatus, formatTime, STEPS, stepFor } from "./model";
import { AppSelectStep } from "./AppSelectStep";
import "./prepare.css";

function useServerClock(snapshot: ConnectionSnapshot | null) {
  const [now, setNow] = useState(snapshot?.serverNow ?? Date.now());
  useEffect(() => {
    const origin = performance.now(),
      server = snapshot?.serverNow ?? Date.now();
    const update = () => setNow(server + performance.now() - origin);
    update();
    const timer = setInterval(update, 500);
    document.addEventListener("visibilitychange", update);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", update);
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
    context: CollectionContext,
  ) => ReactNode;
}) {
  const snapshots = useSyncExternalStore(gateway.subscribe, gateway.list);
  const snapshot = snapshots.find((s) => s.id === requestId) ?? null;
  const [acknowledged, setAcknowledged] = useState(false),
    [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(""),
    [error, setError] = useState(""),
    [confirmRevoke, setConfirmRevoke] = useState(false);
  // 준비 단계: 1 환경 연결 → 2 앱 선택. 환경을 등록한 뒤에만 2 로 갈 수 있다
  const [phase, setPhase] = useState<"connect" | "apps">("connect");
  const lock = useRef(false),
    alive = useRef(true),
    heading = useRef<HTMLDivElement>(null);
  const busyId = useRef(requestId),
    copiedId = useRef<string | undefined>(undefined);
  const now = useServerClock(snapshot),
    step = stepFor(snapshot);
  const stale = Boolean(snapshot && now - snapshot.observedAt > 20_000);
  const context = { organization: snapshot?.organization ?? team };
  const previousView = useRef("");
  const view =
    snapshot?.device === "REVOKED"
      ? "revoked"
      : snapshot?.environmentId
        ? "registered"
        : step === 1
          ? "register"
          : snapshot?.device === "PENDING_APPROVAL"
            ? "confirm"
            : snapshot?.tunnel === "CONNECTING"
              ? "connecting"
              : snapshot?.tunnel === "READY"
                ? "source"
                : "command";
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    busyId.current = requestId;
    if (copiedId.current !== requestId) {
      setError("");
      setMessage("");
    }
    setConfirmRevoke(false);
    setPhase("connect");
  }, [requestId]);
  useEffect(() => {
    if (previousView.current && previousView.current !== view)
      heading.current?.focus({ preventScroll: true });
    previousView.current = view;
  }, [view]);
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
              : "요청을 처리하지 못했어요. 다시 확인하세요.",
          );
      })
      .finally(() => {
        lock.current = false;
        if (alive.current) setBusy(false);
      });
  };
  const startNew = () => {
    if (busy) return;
    setAcknowledged(false);
    setError("");
    setMessage("");
    onNavigate("#prepare");
  };
  function copy() {
    if (lock.current || !acknowledged || !gateway.available || stale) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    const startedId = requestId;
    void copyEnrollmentCommand(gateway, context, snapshot?.id)
      .then((s) => {
        if (!alive.current || busyId.current !== startedId) return;
        copiedId.current = s.id;
        onNavigate("#prepare/" + encodeURIComponent(s.id));
        setMessage(
          "미리보기 안내를 복사했어요. 실제 실행 명령은 복사하지 않았어요.",
        );
      })
      .catch((e) => {
        if (!alive.current || busyId.current !== startedId) return;
        if (e instanceof CopyFailure && e.request) {
          copiedId.current = e.request.id;
          onNavigate("#prepare/" + encodeURIComponent(e.request.id));
        }
        setError(e instanceof Error ? e.message : "복사하지 못했어요.");
      })
      .finally(() => {
        lock.current = false;
        if (alive.current) setBusy(false);
      });
  }
  const status = snapshot ? connectionStatus(snapshot) : null;
  const registered = Boolean(snapshot?.environmentId && snapshot.target);
  const apps = phase === "apps" && registered;
  const source = snapshot?.collected.find(
    (c) => c.id === snapshot.selectedEnvironmentId,
  );
  return (
    <div className={`pk-page connection-page${apps ? " is-apps" : ""}`}>
      <PageHeader title="마이그레이션 준비" />
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
        <li aria-current={apps ? undefined : "step"}>
          <span>1</span>환경 연결
        </li>
        <li aria-current={apps ? "step" : undefined}>
          <span>2</span>앱 선택 {!registered && <small>환경등록 뒤</small>}
        </li>
      </ol>
      {!gateway.available && (
        <p className="connection-notice connection-unavailable" role="status">
          {gateway.reason}
        </p>
      )}
      {apps && snapshot?.target && (
        <>
          <AppSelectStep
            sourceName={snapshot.target.name}
            namespace={source?.context ?? "default"}
          />
          <div className="appsel-bar">
            <span className="appsel-bar-status">
              <StatusDot tone="ok">
                {snapshot.target.name} 환경등록 완료
              </StatusDot>
            </span>
            <button className="secondary" onClick={() => setPhase("connect")}>
              이전: 환경 연결
            </button>
            <button className="primary" disabled>
              다음: 사전 점검 (준비 중)
            </button>
          </div>
        </>
      )}
      <div className="connection-layout" aria-busy={busy} hidden={apps}>
        <aside className="connection-sidebar" aria-label="연결 환경">
          <div className="connection-sidebar-heading">
            <h2>연결 환경</h2>
            <span>
              {snapshots.filter((s) => s.environmentId).length}개 등록
            </span>
          </div>
          <button
            className="secondary connection-add"
            disabled={busy}
            onClick={startNew}
          >
            <Icon name="layers" size={16} />
            환경 연결 추가
          </button>
          {snapshots.length === 0 ? (
            <p className="connection-empty">
              등록한 환경이 없어요.
              <br />
              환경정보 수집부터 시작하세요.
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
                      <strong>{s.target?.name ?? "수집 중인 환경"}</strong>
                      <span>
                        {s.target
                          ? s.target.role === "source"
                            ? "출발 환경"
                            : "도착 환경"
                          : "환경 이름, 역할 미등록"}
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
            <h3>정보 수집 후 환경등록</h3>
            <p>
              단말 확인과 보안 연결은 수집 단계에서 진행해요. 마이그레이션
              범위와 실행 계획은 앱 선택에서 검토해요.
            </p>
          </div>
        </aside>
        <div className="connection-main">
          <div className="connection-main-heading">
            <div>
              <h2>{snapshot?.target?.name ?? "새 환경 연결"}</h2>
              {snapshot?.target && (
                <p>
                  {snapshot.organization} / {snapshot.target.project}
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
                {formatTime(snapshot?.observedAt)}. 연결 확인 전까지 수집과
                등록을 멈춰요.
              </p>
            )}
            {requestId && !snapshot ? (
              <section className="connection-title">
                <h2>수집 요청을 찾지 못했어요</h2>
                <p>종료되었거나 이전 형식의 미리보기 요청일 수 있어요.</p>
                <button className="secondary" onClick={startNew}>
                  새 환경정보 수집
                </button>
              </section>
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
                <fieldset
                  className="connection-stage-content"
                  disabled={busy || stale}
                >
                  {snapshot?.device === "REVOKED" ? (
                    <CleanupResult snapshot={snapshot} />
                  ) : snapshot?.environmentId ? (
                    <RegisteredEnvironment
                      snapshot={snapshot}
                      onNext={registered ? () => setPhase("apps") : undefined}
                    />
                  ) : step === 1 && snapshot ? (
                    <ConnectionTargetForm
                      key={snapshot.id + ":" + snapshot.inventoryDigest}
                      snapshot={snapshot}
                      busy={busy || stale}
                      onRegister={(sourceId, target) =>
                        run(
                          () =>
                            gateway.register(
                              snapshot.id,
                              sourceId,
                              snapshot.inventoryDigest!,
                              target,
                            ),
                          "환경을 등록했어요.",
                        )
                      }
                    />
                  ) : snapshot?.device === "PENDING_APPROVAL" ||
                    snapshot?.device === "REJECTED" ? (
                    <DeviceApproval
                      key={snapshot.id}
                      snapshot={snapshot}
                      gateway={gateway}
                      now={now}
                      busy={busy || stale}
                      run={run}
                    />
                  ) : snapshot?.device === "APPROVED" &&
                    snapshot.tunnel === "READY" ? (
                    <CollectionSource
                      key={snapshot.id + ":" + snapshot.epoch}
                      snapshot={snapshot}
                      busy={busy || stale}
                      onCollect={(sourceId) =>
                        run(
                          () => gateway.collect(snapshot.id, sourceId),
                          "환경정보를 수집했어요.",
                        )
                      }
                    />
                  ) : snapshot?.device === "APPROVED" ? (
                    <TunnelProgress snapshot={snapshot} />
                  ) : (
                    <CollectionStart
                      snapshot={snapshot}
                      available={gateway.available && !stale}
                      preview={gateway.mode === "preview"}
                      now={now}
                      busy={busy}
                      acknowledged={acknowledged}
                      onAcknowledge={setAcknowledged}
                      onCopy={copy}
                    />
                  )}
                </fieldset>
                {snapshot?.device === "APPROVED" && (
                  <section className="connection-management">
                    <h3>연결 관리</h3>
                    <p>
                      마지막 확인 {formatTime(snapshot.observedAt)}, 키{" "}
                      {snapshot.epoch}세대
                      {snapshot.keyExpiresAt
                        ? ", 만료 " + formatTime(snapshot.keyExpiresAt)
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
                            새 수집 요청과 터널을 차단할까요? 고객 권한과 단말
                            파일 정리는 별도로 확인해요.
                          </p>
                          <button
                            className="connection-danger"
                            disabled={busy}
                            onClick={() =>
                              run(
                                () => gateway.revoke(snapshot.id),
                                "연결을 해제했어요.",
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
                        onClick={startNew}
                      >
                        새 환경정보 수집
                      </button>
                    </div>
                  )}
                {snapshot && (
                  <details className="connection-details connection-history">
                    <summary>
                      수집/등록 기록 <span>{snapshot.events.length}건</span>
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
          {previewTools?.(snapshot, context)}
        </div>
      </div>
    </div>
  );
}
