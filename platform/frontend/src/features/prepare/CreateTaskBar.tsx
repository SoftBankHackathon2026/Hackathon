import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { errorMessage } from "../../api/client";
import { createMigration } from "../../api/taskPlan";
import { fleetQueryOptions } from "../migrations/queries";
import type { ConnectionSnapshot } from "./connectionTypes";
import { taskRequest, type TaskEnvironment } from "./taskRoute";

function workloadApps(snapshot: ConnectionSnapshot): string[] {
  return snapshot.workloads?.coverage === "complete"
    ? snapshot.workloads.items.map(
        ({ namespace, kind, name }) => `${namespace}/${kind}/${name}`,
      )
    : [];
}

export function CreateTaskBar({
  prepared,
  connections,
  apps,
  onBack,
  onCreated,
}: {
  prepared: ConnectionSnapshot;
  connections: readonly ConnectionSnapshot[];
  apps: string[];
  onBack: () => void;
  onCreated: (jobId: string) => void;
}) {
  const fleet = useQuery(fleetQueryOptions());
  const environments = new Map<string, TaskEnvironment & { apps: string[] }>();
  for (const env of fleet.data?.environments ?? []) {
    environments.set(env.id, { ...env, apps: env.apps.map((app) => app.name) });
  }
  for (const connection of connections) {
    const { environmentId, target, device } = connection;
    if (!environmentId || !target || device !== "APPROVED") continue;
    environments.set(environmentId, {
      id: environmentId,
      name: target.name,
      provider: target.kind === "kubernetes" ? "onprem" : target.kind,
      apps: workloadApps(connection),
    });
  }

  const [sourceId, setSource] = useState("");
  const [targetId, setTarget] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const source = environments.get(sourceId);
  const target = environments.get(targetId);
  const preparedSource = sourceId === prepared.environmentId;
  const sourceApps = [...new Set(source?.apps ?? [])];
  const selectedApps = (preparedSource ? apps : selected).filter((app) =>
    sourceApps.includes(app),
  );
  const create = useMutation({
    mutationFn: () => {
      if (
        !source ||
        !target ||
        source.id === target.id ||
        !selectedApps.length ||
        selectedApps.length > 50
      ) {
        throw new Error("출발·도착 환경과 옮길 앱을 확인해 주세요.");
      }
      return createMigration(taskRequest(source, target, selectedApps));
    },
    onSuccess: (created) => onCreated(created.job_id),
  });
  const options = [...environments.values()];

  return (
    <form
      className="pk-section connection-task-form"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate();
      }}
    >
      <p>이 작업에 사용할 출발 환경과 도착 환경을 지정하세요.</p>
      <fieldset className="connection-fields" disabled={create.isPending}>
        <label>
          출발 환경
          <select
            value={sourceId}
            required
            onChange={(event) => {
              const id = event.target.value;
              setSource(id);
              setSelected([]);
              if (id === targetId) setTarget("");
              create.reset();
            }}
          >
            <option value="">출발 환경을 선택하세요</option>
            {options.map((env) => (
              <option key={env.id} value={env.id}>
                {env.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          도착 환경
          <select
            value={targetId}
            required
            onChange={(event) => {
              setTarget(event.target.value);
              create.reset();
            }}
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
      {fleet.isError && (
        <p role="status">
          기존 환경 목록을 불러오지 못했어요. 직접 등록한 환경을 선택할 수
          있어요.
        </p>
      )}
      {source && !preparedSource && sourceApps.length > 0 && (
        <fieldset className="connection-task-apps" disabled={create.isPending}>
          <legend>출발 환경에서 옮길 앱</legend>
          {sourceApps.map((app) => (
            <label key={app}>
              <input
                type="checkbox"
                checked={selected.includes(app)}
                onChange={(event) => {
                  const checked = event.target.checked;
                  setSelected((current) =>
                    checked
                      ? [...current, app]
                      : current.filter((item) => item !== app),
                  );
                }}
              />
              {app}
            </label>
          ))}
        </fieldset>
      )}
      {source && !sourceApps.length && (
        <p role="status">
          이 출발 환경에서 확인한 앱이 없어요. 환경에서 앱 정보를 먼저 수집해
          주세요.
        </p>
      )}
      {preparedSource && sourceApps.length > 0 && !selectedApps.length && (
        <p role="status">준비에서 선택한 앱을 다시 확인해 주세요.</p>
      )}
      {selectedApps.length > 50 && (
        <p role="status">한 작업에서 앱은 최대 50개까지 선택할 수 있어요.</p>
      )}
      <div className="connection-actions">
        <button
          type="button"
          className="secondary"
          onClick={onBack}
          disabled={create.isPending}
        >
          이전: 앱 선택
        </button>
        <button
          type="submit"
          className="primary"
          disabled={
            !source ||
            !target ||
            !selectedApps.length ||
            selectedApps.length > 50 ||
            create.isPending
          }
        >
          {create.isPending
            ? "작업 만드는 중…"
            : `작업 만들기 (앱 ${selectedApps.length}개)`}
        </button>
      </div>
      {create.isError && (
        <p role="alert">
          {errorMessage(create.error, "작업을 만들지 못했어요.")}
        </p>
      )}
    </form>
  );
}
