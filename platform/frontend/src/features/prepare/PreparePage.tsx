import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "../../components/PageKit";
import { StatusDot } from "../../components/StatusDot";
import { Icon } from "../../components/Icon";
import { createLiveConnections } from "../../api/liveConnections";
import type {
  CollectionContext,
  ConnectionGateway,
  ConnectionSnapshot,
  Provider,
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
import { fleetQueryOptions } from "../migrations/queries";
import {
  connectionStatus,
  existingEnvironments,
  formatTime,
  isListedConnection,
  STEPS,
  PROVIDERS,
  stepFor,
} from "./model";
import {
  TaskRegistration,
  type TaskStep,
  TASK_STEPS,
} from "./TaskRegistration";
import { transitionPreparePanel } from "./transitionPreparePanel";
import "./prepare.css";

function useServerClock(snapshot: ConnectionSnapshot | null) {
  const [now, setNow] = useState(snapshot?.serverNow ?? Date.now());
  const server = snapshot?.serverNow;
  useEffect(() => {
    if (server === undefined) return;
    const origin = performance.now();
    const update = () => setNow(server + performance.now() - origin);
    update();
    const timer = setInterval(update, 500);
    document.addEventListener("visibilitychange", update);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", update);
    };
  }, [server, snapshot?.id]);
  return now;
}

export function PreparePage({
  onNavigate,
  requestId,
  newConnection = false,
  team = "",
  gateway: providedGateway,
  previewTools,
}: {
  onNavigate: (hash: string) => void;
  requestId?: string;
  newConnection?: boolean;
  team?: string;
  gateway?: ConnectionGateway;
  previewTools?: (
    snapshot: ConnectionSnapshot | null,
    context: CollectionContext,
  ) => ReactNode;
}) {
  const liveGateway = useMemo(() => createLiveConnections(), [team]);
  const gateway = providedGateway ?? liveGateway;
  const snapshots = useSyncExternalStore(gateway.subscribe, gateway.list);
  const listedConnections = useMemo(
    () => snapshots.filter(isListedConnection),
    [snapshots],
  );
  const fleet = useQuery({
    ...fleetQueryOptions(),
    enabled: gateway.mode !== "preview",
  });
  const existing = existingEnvironments(fleet.data?.environments);
  const snapshot = snapshots.find((s) => s.id === requestId) ?? null;
  const [acknowledged, setAcknowledged] = useState(false),
    [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(""),
    [error, setError] = useState(""),
    [disconnectId, setDisconnectId] = useState<string>();
  const disconnectDialog = useRef<HTMLDialogElement>(null);
  const disconnectTarget = listedConnections.find((s) => s.id === disconnectId);
  const panelId = requestId ?? (newConnection ? "new" : undefined);
  const managingConnection = Boolean(panelId);
  const [taskStep, setTaskStep] = useState<TaskStep>("environments");
  const [provider, setProvider] = useState<Provider>("kubernetes");
  const lock = useRef(false),
    alive = useRef(true),
    heading = useRef<HTMLDivElement>(null);
  const busyId = useRef(panelId),
    copiedId = useRef<string | undefined>(undefined);
  const now = useServerClock(snapshot),
    step = stepFor(snapshot);
  const stale = Boolean(snapshot && now - snapshot.observedAt > 20_000);
  const context = { organization: snapshot?.organization ?? team };
  const previousView = useRef("");
  const view = !managingConnection
    ? taskStep
    : !snapshot
      ? requestId
        ? "missing"
        : "new"
      : snapshot?.device === "REVOKED"
        ? "revoked"
        : snapshot?.environmentId
          ? "registered"
          : step === 1
            ? "register"
            : snapshot.registration === "CANCELLED"
              ? "cancelled"
              : snapshot.device === "PENDING_APPROVAL" ||
                  snapshot.device === "REJECTED"
                ? "confirm"
                : snapshot.device === "APPROVED"
                  ? snapshot.tunnel === "READY"
                    ? "source"
                    : "tunnel"
                  : "command";
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    busyId.current = panelId;
    if (copiedId.current !== requestId) {
      setError("");
      setMessage("");
    }
    disconnectDialog.current?.close();
    setDisconnectId(undefined);
  }, [panelId, requestId]);
  useEffect(() => {
    const next = (requestId ?? "") + ":" + view;
    if (previousView.current && previousView.current !== next)
      heading.current?.focus({ preventScroll: true });
    previousView.current = next;
  }, [requestId, view]);
  const run = (action: () => Promise<void>, success = "") => {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    const startedId = panelId;
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
  const canDisconnect = (s: ConnectionSnapshot) =>
    gateway.available && s.permissions.revoke && isListedConnection(s);
  const closeDisconnect = () => {
    disconnectDialog.current?.close();
    setDisconnectId(undefined);
  };
  const openDisconnect = (
    s: ConnectionSnapshot,
    trigger: HTMLButtonElement,
  ) => {
    setError("");
    setDisconnectId(s.id);
    trigger.focus({ preventScroll: true });
    disconnectDialog.current?.showModal();
  };
  const disconnect = () => {
    const s = disconnectTarget;
    if (!s || !canDisconnect(s)) return;
    const approved = s.device === "APPROVED";
    const pending = s.device === "PENDING_APPROVAL";
    run(
      async () => {
        if (approved) await gateway.revoke(s.id);
        else if (pending) await gateway.reject(s.id);
        else await gateway.cancel(s.id);
        closeDisconnect();
        if (alive.current) heading.current?.focus({ preventScroll: true });
      },
      approved
        ? "서버 권한을 철회했어요. 터널과 단말 정리는 별도로 확인해요."
        : "연결 요청을 취소했어요.",
    );
  };
  const navigatePanel = (hash: string) => {
    if (window.location.hash !== hash)
      transitionPreparePanel(() => onNavigate(hash));
  };
  const returnToTask = () => navigatePanel("#prepare");
  const startNew = () => {
    if (busy) return;
    setAcknowledged(false);
    setProvider("kubernetes");
    setError("");
    setMessage("");
    navigatePanel("#prepare/new");
  };
  function copy() {
    if (lock.current || !acknowledged || !gateway.available || stale) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    const startedId = panelId;
    void copyEnrollmentCommand(
      gateway,
      { ...context, provider: snapshot?.provider ?? provider },
      snapshot?.id,
    )
      .then((s) => {
        if (!alive.current || busyId.current !== startedId) return;
        copiedId.current = s.id;
        navigatePanel("#prepare/" + encodeURIComponent(s.id));
        setMessage(
          gateway.mode === "preview"
            ? "미리보기 안내를 복사했어요. 실제 실행 명령은 복사하지 않았어요."
            : "명령을 복사했어요. 준비한 단말에서 기한 안에 실행하세요.",
        );
      })
      .catch((e) => {
        if (!alive.current || busyId.current !== startedId) return;
        if (e instanceof CopyFailure && e.request) {
          copiedId.current = e.request.id;
          navigatePanel("#prepare/" + encodeURIComponent(e.request.id));
        }
        setError(e instanceof Error ? e.message : "복사하지 못했어요.");
      })
      .finally(() => {
        lock.current = false;
        if (alive.current) setBusy(false);
      });
  }
  const status = snapshot ? connectionStatus(snapshot) : null;
  return (
    <div className="pk-page connection-page">
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
      <ol className="connection-phases" aria-label="작업 등록 단계">
        {TASK_STEPS.map(({ id, label }, index) => (
          <li
            key={id}
            aria-current={
              !managingConnection && id === taskStep ? "step" : undefined
            }
          >
            <span>{index + 1}</span>
            {label}
          </li>
        ))}
      </ol>
      <div className="connection-layout">
        <aside className="connection-sidebar" aria-label="연결 환경">
          <div className="connection-sidebar-heading">
            <h2>연결 환경</h2>
            <span>
              기존 {existing.length}개, 직접 연결{" "}
              {listedConnections.filter((s) => s.environmentId).length}개
            </span>
          </div>
          <button
            className="secondary connection-add"
            aria-current={newConnection ? "true" : undefined}
            disabled={busy}
            onClick={startNew}
          >
            <Icon name="layers" size={16} />
            환경 연결 추가
          </button>
          {managingConnection &&
            (!snapshot?.environmentId ||
              stale ||
              busy ||
              snapshot.device !== "APPROVED") && (
              <button
                className="secondary connection-return"
                onClick={returnToTask}
              >
                작업 등록으로 돌아가기
              </button>
            )}
          {existing.length > 0 && (
            <>
              <h3 className="connection-group-title">기존 환경</h3>
              <ul className="connection-environments" aria-label="기존 환경">
                {existing.map((env) => (
                  <li key={env.id} className="is-static">
                    <div className="connection-environment-select">
                      <strong>{env.name}</strong>
                      <span>{env.detail}</span>
                      <StatusDot tone={env.tone}>{env.label}</StatusDot>
                    </div>
                  </li>
                ))}
              </ul>
              <h3 className="connection-group-title">직접 연결</h3>
            </>
          )}
          {listedConnections.length === 0 ? (
            <p className="connection-empty">직접 추가한 연결이 없어요.</p>
          ) : (
            <ul className="connection-environments">
              {listedConnections.map((s) => {
                const state = connectionStatus(s);
                const name =
                  s.target?.name ?? s.collected[0]?.name ?? "환경 연결 요청";
                return (
                  <li key={s.id}>
                    <button
                      className="connection-environment-select"
                      disabled={busy}
                      aria-current={s.id === requestId ? "true" : undefined}
                      onClick={() =>
                        navigatePanel("#prepare/" + encodeURIComponent(s.id))
                      }
                    >
                      <strong>{name}</strong>
                      <span>
                        {
                          PROVIDERS.find(
                            (p) =>
                              p.value ===
                              (s.target?.kind ?? s.provider ?? "kubernetes"),
                          )?.title
                        }
                      </span>
                      <StatusDot tone={state.tone}>{state.label}</StatusDot>
                    </button>
                    <button
                      type="button"
                      className="connection-disconnect"
                      aria-label={`${name} 연결 해제`}
                      title="연결 해제"
                      disabled={busy || !canDisconnect(s)}
                      onClick={(event) =>
                        openDisconnect(s, event.currentTarget)
                      }
                    >
                      <Icon name="unlink" size={18} />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {!managingConnection && message && (
            <p className="connection-success prepare-state-enter" role="status">
              {message}
            </p>
          )}
          <div className="connection-sidebar-note">
            <Icon name="shield" size={18} />
            <h3>환경 연결과 작업 등록</h3>
            <p>수집한 환경을 목록에 저장하면 작업에 사용할 수 있어요.</p>
          </div>
        </aside>
        <div className="prepare-panel">
          <div hidden={managingConnection}>
            <TaskRegistration
              connections={listedConnections}
              gateway={gateway}
              connectionBusy={busy}
              step={taskStep}
              onStep={setTaskStep}
              onAddConnection={startNew}
              onCreated={(jobId) =>
                onNavigate("#migrations/" + encodeURIComponent(jobId))
              }
              headingRef={managingConnection ? undefined : heading}
            />
          </div>
          {managingConnection && (
            <div
              className="connection-main prepare-panel-enter"
              key={requestId ?? "new"}
              aria-busy={busy}
            >
              {!gateway.available && (
                <p
                  className="connection-notice connection-unavailable"
                  role="status"
                >
                  {gateway.reason}
                </p>
              )}
              <div className="connection-main-heading">
                <div>
                  <h2>
                    {snapshot?.target?.name ??
                      snapshot?.collected[0]?.name ??
                      (requestId ? "환경 연결 상세" : "새 환경 연결")}
                  </h2>
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
              {!snapshot?.environmentId && (
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
              )}
              <div ref={heading} tabIndex={-1} className="connection-work">
                <div className="connection-feedback" aria-live="polite">
                  {message && (
                    <p className="connection-success prepare-state-enter">
                      {message}
                    </p>
                  )}
                </div>
                {error && !disconnectId && (
                  <p
                    className="connection-error prepare-state-enter"
                    role="alert"
                  >
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
                      <p
                        className="connection-error prepare-state-enter"
                        role="alert"
                      >
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
                      <div key={view} className="prepare-state-enter">
                        {snapshot?.device === "REVOKED" ? (
                          <CleanupResult snapshot={snapshot} />
                        ) : snapshot?.registration === "CANCELLED" ? (
                          <section className="connection-title">
                            <h2>연결 요청을 취소했어요</h2>
                          </section>
                        ) : snapshot?.environmentId ? (
                          <RegisteredEnvironment
                            snapshot={snapshot}
                            onReturn={returnToTask}
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
                            replacements={
                              gateway.mode === "live" &&
                              snapshot.candidate?.supportsReplacement
                                ? snapshots.filter(
                                    (old) =>
                                      old.id !== snapshot.id &&
                                      old.organization ===
                                        snapshot.organization &&
                                      old.device === "APPROVED" &&
                                      old.permissions.revoke &&
                                      (!old.replacement ||
                                        old.replacement.client === "ready"),
                                  )
                                : []
                            }
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
                            onRetry={
                              gateway.rediscover
                                ? () =>
                                    run(() => gateway.rediscover!(snapshot.id))
                                : undefined
                            }
                            onSkip={startNew}
                            onCollect={(sourceId) =>
                              run(
                                () => gateway.collect(snapshot.id, sourceId),
                                "환경정보 수집을 요청했어요. 단말의 결과를 기다려 주세요.",
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
                            provider={snapshot?.provider ?? provider}
                            providers={gateway.providers ?? ["kubernetes"]}
                            onProvider={(next) => {
                              setProvider(next);
                              setAcknowledged(false);
                            }}
                          />
                        )}
                      </div>
                    </fieldset>
                    {snapshot?.device === "APPROVED" && (
                      <section className="connection-management">
                        <h3>연결 관리</h3>
                        <p>
                          마지막 단말 응답{" "}
                          {snapshot.lastTunnelActivityAt
                            ? formatTime(snapshot.lastTunnelActivityAt)
                            : "기록 없음"}
                          {snapshot.epoch > 0 &&
                            `, 연결 키 ${snapshot.epoch}세대`}
                          {snapshot.keyExpiresAt
                            ? ", 만료 " + formatTime(snapshot.keyExpiresAt)
                            : ""}
                        </p>
                        {snapshot.tunnel === "DEGRADED" &&
                          gateway.mode === "live" && (
                            <p>
                              온프레미스에서 연결 서비스가 실행 중인지 확인해
                              주세요. 중지된 서비스는 단말에서{" "}
                              <code>
                                sudo systemctl start migration-connector
                              </code>
                              로 시작할 수 있어요. 이전 버전은 기존 요청의 재개
                              명령을 사용해 주세요.
                            </p>
                          )}
                        <div className="connection-actions">
                          {snapshot.tunnel === "DEGRADED" && (
                            <button
                              className="primary"
                              disabled={busy || stale}
                              onClick={() =>
                                run(
                                  () => gateway.reconnect(snapshot.id),
                                  "재접속을 요청했어요. 단말 응답을 기다려 주세요.",
                                )
                              }
                            >
                              새 연결로 재접속
                            </button>
                          )}
                          <button
                            className="secondary"
                            disabled={busy || !canDisconnect(snapshot)}
                            onClick={(event) =>
                              openDisconnect(snapshot, event.currentTarget)
                            }
                          >
                            연결 해제
                          </button>
                        </div>
                      </section>
                    )}
                    {snapshot &&
                      (snapshot.registration === "EXPIRED" ||
                        snapshot.registration === "CANCELLED" ||
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
          )}
        </div>
      </div>
      <dialog
        ref={disconnectDialog}
        className="connection-revoke"
        aria-labelledby="connection-revoke-title"
        aria-describedby="connection-revoke-description"
        onCancel={(event) => {
          event.preventDefault();
          if (!busy) closeDisconnect();
        }}
      >
        <h2 id="connection-revoke-title">연결을 해제할까요?</h2>
        <p id="connection-revoke-description">
          {disconnectTarget?.target?.name && (
            <strong>{disconnectTarget.target.name} · </strong>
          )}
          {disconnectTarget?.device === "APPROVED"
            ? "새 수집 요청과 터널을 차단해요. 고객 권한과 단말 파일 정리는 별도로 확인해요."
            : disconnectTarget
              ? "이 환경의 연결 요청을 취소해요."
              : "이 연결 요청은 이미 종료되었어요."}
        </p>
        {error && disconnectId && (
          <p className="connection-error prepare-state-enter" role="alert">
            {error}
          </p>
        )}
        <div className="connection-actions">
          <button
            type="button"
            className="secondary"
            autoFocus
            disabled={busy}
            onClick={closeDisconnect}
          >
            취소
          </button>
          <button
            type="button"
            className="connection-danger"
            disabled={
              busy || !disconnectTarget || !canDisconnect(disconnectTarget)
            }
            onClick={disconnect}
          >
            {busy ? "연결 해제 중…" : "연결 해제 확인"}
          </button>
        </div>
      </dialog>
    </div>
  );
}
