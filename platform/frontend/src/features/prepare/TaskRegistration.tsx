import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type Ref } from "react";
import { errorMessage } from "../../api/client";
import { createMigration } from "../../api/taskPlan";
import { fleetQueryOptions } from "../migrations/queries";
import { AppSelectStep } from "./AppSelectStep";
import type { ConnectionGateway, ConnectionSnapshot } from "./connectionTypes";
import { LiveAppSelect } from "./LiveAppSelect";
import { taskRequest, type TaskEnvironment } from "./taskRoute";
import { transitionPreparePanel } from "./transitionPreparePanel";

export const TASK_STEPS = [
  { id: "environments", label: "출발·도착 환경 선택" },
  { id: "apps", label: "앱 선택" },
  { id: "review", label: "작업 등록" },
] as const;

export type TaskStep = (typeof TASK_STEPS)[number]["id"];

type TaskOption = TaskEnvironment & {
  apps: string[];
  connection?: ConnectionSnapshot;
};

export function TaskRegistration({
  connections,
  gateway,
  connectionBusy,
  step,
  onStep,
  onAddConnection,
  onCreated,
  headingRef,
}: {
  connections: readonly ConnectionSnapshot[];
  gateway: ConnectionGateway;
  connectionBusy: boolean;
  step: TaskStep;
  onStep: (step: TaskStep) => void;
  onAddConnection: () => void;
  onCreated: (jobId: string) => void;
  headingRef?: Ref<HTMLDivElement>;
}) {
  const preview = gateway.mode === "preview";
  const fleet = useQuery({ ...fleetQueryOptions(), enabled: !preview });
  const queryClient = useQueryClient();
  const environments = new Map<string, TaskOption>();
  for (const env of fleet.data?.environments ?? []) {
    environments.set(env.id, { ...env, apps: env.apps.map((app) => app.name) });
  }
  if (preview) {
    environments.set("preview-destination", {
      id: "preview-destination",
      name: "예시 도착 환경",
      provider: "gcp",
      apps: [],
    });
  }
  for (const connection of connections) {
    const { environmentId, target, device } = connection;
    if (!environmentId || !target || device !== "APPROVED") continue;
    const inventory = connection.workloads;
    const apps =
      connection.collectionPending || inventory?.coverage !== "complete"
        ? []
        : inventory.items.map(
            ({ namespace, kind, name }) => `${namespace}/${kind}/${name}`,
          );
    environments.set(environmentId, {
      id: environmentId,
      name: target.name,
      provider: target.kind === "kubernetes" ? "onprem" : target.kind,
      connection,
      apps,
    });
  }

  const [sourceId, setSource] = useState("");
  const [targetId, setTarget] = useState("");
  const [selection, setSelection] = useState<{
    inventory?: string;
    apps: string[];
  }>({ apps: [] });
  const source = environments.get(sourceId);
  const target = environments.get(targetId);
  const connectedSource = source?.connection;
  const inventory = connectedSource?.inventoryDigest;
  const sourceApps = [...new Set(source?.apps ?? [])];
  const previewApps = preview && Boolean(connectedSource);
  if (selection.inventory !== inventory) {
    setSelection({ inventory, apps: [] });
  }
  const refresh = useMutation({
    mutationFn: (connection: ConnectionSnapshot) => {
      if (!connection.selectedEnvironmentId) {
        throw new Error("수집 대상을 확인할 수 없어요.");
      }
      return gateway.collect(connection.id, connection.selectedEnvironmentId);
    },
  });
  const refreshing =
    refresh.isPending && refresh.variables?.id === connectedSource?.id;
  const selected =
    selection.inventory === inventory && !refreshing
      ? selection.apps.filter((app) => previewApps || sourceApps.includes(app))
      : [];
  const distinctEnvironments = Boolean(
    source && target && source.id !== target.id,
  );
  const validApps = selected.length > 0 && selected.length <= 50;
  const submitting = useRef(false);
  const create = useMutation({
    mutationFn: () => {
      if (
        preview ||
        !source ||
        !target ||
        !distinctEnvironments ||
        !validApps
      ) {
        throw new Error("출발·도착 환경과 옮길 앱을 확인해 주세요.");
      }
      return createMigration(taskRequest(source, target, selected));
    },
    onSuccess: (created) => {
      void queryClient.invalidateQueries({
        queryKey: fleetQueryOptions().queryKey,
      });
      onCreated(created.job_id);
    },
    onSettled: () => {
      submitting.current = false;
    },
  });
  const options = [...environments.values()];
  const changeStep = (next: TaskStep) => {
    create.reset();
    transitionPreparePanel(() => onStep(next));
  };
  const selectApps = (apps: string[]) => setSelection({ inventory, apps });
  const title = TASK_STEPS.find((item) => item.id === step)?.label;
  const unavailableSelection = (sourceId && !source) || (targetId && !target);

  return (
    <div className="connection-main prepare-panel-enter" key={step}>
      <div className="connection-main-heading" ref={headingRef} tabIndex={-1}>
        <h2>{step === "environments" ? "마이그레이션 작업 등록" : title}</h2>
      </div>
      <form
        className="connection-task-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (step === "environments" && distinctEnvironments)
            changeStep("apps");
          if (step === "apps" && distinctEnvironments && validApps)
            changeStep("review");
          if (
            step === "review" &&
            !preview &&
            distinctEnvironments &&
            validApps &&
            !submitting.current
          ) {
            submitting.current = true;
            create.mutate();
          }
        }}
      >
        {step === "environments" && (
          <>
            <p>작업에 사용할 출발 환경과 도착 환경을 선택하세요.</p>
            <fieldset className="connection-fields">
              <label>
                출발 환경
                <select
                  value={source?.id ?? ""}
                  required
                  onChange={(event) => {
                    const id = event.target.value;
                    setSource(id);
                    const next = environments.get(id)?.connection;
                    setSelection({
                      inventory: next?.inventoryDigest,
                      apps: preview && next ? ["sample-app"] : [],
                    });
                    if (id === targetId) setTarget("");
                  }}
                >
                  <option value="">출발 환경을 선택하세요</option>
                  {options
                    .filter((env) => !preview || env.connection)
                    .map((env) => (
                      <option key={env.id} value={env.id}>
                        {env.name}
                      </option>
                    ))}
                </select>
              </label>
              <label>
                도착 환경
                <select
                  value={target?.id ?? ""}
                  required
                  onChange={(event) => setTarget(event.target.value)}
                >
                  <option value="">도착 환경을 선택하세요</option>
                  {options
                    .filter((env) => env.id !== sourceId)
                    .map((env) => (
                      <option key={env.id} value={env.id}>
                        {env.name}
                      </option>
                    ))}
                </select>
              </label>
            </fieldset>
            {!preview && fleet.isPending && (
              <p className="prepare-state-enter" role="status">
                환경 목록을 불러오고 있어요.
              </p>
            )}
            {fleet.isError && (
              <p className="prepare-state-enter" role="status">
                기존 환경 목록을 불러오지 못했어요. 직접 저장한 환경은 선택할 수
                있어요.
              </p>
            )}
            {(preview
              ? !options.some((env) => env.connection)
              : !fleet.isPending && options.length < 2) && (
              <p className="prepare-state-enter" role="status">
                선택할 환경이 부족해요.{" "}
                <button
                  type="button"
                  className="secondary"
                  onClick={onAddConnection}
                  disabled={connectionBusy}
                >
                  환경을 추가하세요
                </button>
              </p>
            )}
          </>
        )}
        {unavailableSelection && (
          <p className="connection-notice prepare-state-enter" role="status">
            선택한 환경을 사용할 수 없어요. 출발·도착 환경을 다시 선택하세요.
          </p>
        )}
        {step === "apps" &&
          source &&
          (previewApps && connectedSource ? (
            <AppSelectStep
              key={source.id}
              sourceName={source.name}
              namespace={
                connectedSource.collected.find(
                  (item) => item.id === connectedSource.selectedEnvironmentId,
                )?.context ?? "default"
              }
              picked={selected}
              onPick={selectApps}
            />
          ) : connectedSource ? (
            <LiveAppSelect
              key={source.id}
              snapshot={connectedSource}
              busy={connectionBusy || refreshing}
              selected={selected}
              onSelect={selectApps}
              onRefresh={() => refresh.mutate(connectedSource)}
              canRefresh={
                gateway.available &&
                Boolean(connectedSource.selectedEnvironmentId)
              }
            />
          ) : sourceApps.length ? (
            <fieldset className="connection-task-apps">
              <legend>출발 환경에서 옮길 앱</legend>
              {sourceApps.map((app) => (
                <label key={app}>
                  <input
                    type="checkbox"
                    checked={selected.includes(app)}
                    onChange={(event) =>
                      selectApps(
                        event.target.checked
                          ? [...selected, app]
                          : selected.filter((item) => item !== app),
                      )
                    }
                  />
                  {app}
                </label>
              ))}
            </fieldset>
          ) : (
            <p className="prepare-state-enter" role="status">
              이 출발 환경에서 확인한 앱이 없어요.
            </p>
          ))}
        {step === "apps" &&
          refresh.isError &&
          refresh.variables?.id === connectedSource?.id && (
            <p className="connection-error prepare-state-enter" role="alert">
              {errorMessage(refresh.error, "앱 목록을 다시 수집하지 못했어요.")}
            </p>
          )}
        {step === "review" && distinctEnvironments && (
          <>
            <dl className="connection-facts">
              <div>
                <dt>출발 환경</dt>
                <dd>{source?.name}</dd>
              </div>
              <div>
                <dt>도착 환경</dt>
                <dd>{target?.name}</dd>
              </div>
              <div>
                <dt>옮길 앱</dt>
                <dd>{selected.length}개</dd>
              </div>
            </dl>
            {selected.length ? (
              <ul className="connection-task-review">
                {selected.map((app) => (
                  <li key={app}>{app}</li>
                ))}
              </ul>
            ) : (
              <p
                className="connection-notice prepare-state-enter"
                role="status"
              >
                수집 결과가 바뀌었어요. 옮길 앱을 다시 선택하세요.
              </p>
            )}
          </>
        )}
        {selected.length > 50 && (
          <p className="connection-notice prepare-state-enter" role="status">
            한 작업에서 앱은 최대 50개까지 선택할 수 있어요.
          </p>
        )}
        <div className="connection-actions">
          {step !== "environments" && (
            <button
              type="button"
              className="secondary"
              onClick={() =>
                changeStep(
                  step === "review" && distinctEnvironments
                    ? "apps"
                    : "environments",
                )
              }
              disabled={create.isPending}
            >
              {step === "review" && distinctEnvironments
                ? "이전: 앱 선택"
                : "이전: 환경 선택"}
            </button>
          )}
          <button
            type="submit"
            className="primary"
            disabled={
              !distinctEnvironments ||
              (step !== "environments" && !validApps) ||
              (step === "review" && preview) ||
              create.isPending
            }
          >
            {step === "environments"
              ? "다음: 앱 선택"
              : step === "apps"
                ? "다음: 작업 등록"
                : preview
                  ? "작업 만들기 (실제 연결에서만)"
                  : create.isPending
                    ? "작업 만드는 중…"
                    : `작업 만들기 (앱 ${selected.length}개)`}
          </button>
        </div>
        {create.isError && (
          <p className="connection-error prepare-state-enter" role="alert">
            {errorMessage(create.error, "작업을 만들지 못했어요.")}
          </p>
        )}
      </form>
    </div>
  );
}
