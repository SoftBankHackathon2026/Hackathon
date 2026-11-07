import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { errorMessage } from "../../api/client";
import { createMigration, type Provider } from "../../api/taskPlan";
import { fleetQueryOptions } from "../migrations/queries";
import { PROVIDER_OPTIONS, taskRequest } from "./taskRoute";

export function CreateTaskBar({
  environmentId,
  role,
  apps,
  onCreated,
}: {
  environmentId: string;
  role: "source" | "target";
  apps: string[];
  onCreated: (jobId: string) => void;
}) {
  const fleet = useQuery(fleetQueryOptions()).data;
  const [provider, setProvider] = useState<Provider>("onprem");
  const [counterpart, setCounterpart] = useState("");
  const create = useMutation({
    mutationFn: () =>
      createMigration(
        taskRequest({ id: environmentId, role, provider }, counterpart, apps),
      ),
    onSuccess: (created) => onCreated(created.job_id),
  });
  const other = role === "source" ? "도착 환경" : "출발 환경";
  return (
    <div className="create-task">
      <label>
        이 환경 종류
        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value as Provider)}
        >
          {PROVIDER_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        {other}
        <select
          value={counterpart}
          onChange={(e) => setCounterpart(e.target.value)}
        >
          <option value="">선택하세요</option>
          {(fleet?.environments ?? []).map((env) => (
            <option key={env.id} value={env.id}>
              {env.name}
            </option>
          ))}
        </select>
      </label>
      <button
        className="primary"
        disabled={!counterpart || apps.length === 0 || create.isPending}
        onClick={() => create.mutate()}
      >
        {create.isPending
          ? "작업 만드는 중…"
          : `작업 만들기 (앱 ${apps.length}개)`}
      </button>
      {create.isError && (
        <p role="alert">
          {errorMessage(create.error, "작업을 만들지 못했어요.")}
        </p>
      )}
    </div>
  );
}
