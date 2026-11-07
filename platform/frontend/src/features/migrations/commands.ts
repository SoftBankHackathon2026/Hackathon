import type {
  CommandAction,
  CommandStatus,
  CutoverCommand,
  StageInfo,
} from "../../api/migrations.ts";
import { stageKey, type Tone } from "./stages.ts";

export const COMMAND_ACTIONS: readonly CommandAction[] = [
  "run",
  "approve",
  "retry",
  "force_retry",
  "abort",
];

export const COMMAND_LABELS: Record<CommandAction, string> = {
  run: "시작",
  approve: "승인",
  retry: "재시도",
  force_retry: "강제 재개",
  abort: "중단·원복",
};

export const COMMAND_HELP: Record<CommandAction, string> = {
  run: "사전 점검부터 전환 승인 대기(READY)까지 진행해요.",
  approve: "승인 ID로 원본 쓰기 차단과 쓰기 전환을 진행해요.",
  retry: "중단된 단계부터 다시 시도해요.",
  force_retry: "복구 필요 상태에서 사람이 확인한 뒤 강제로 다시 시작해요.",
  abort: "쓰기 전환 전에 중단된 작업을 원본 기준으로 되돌려요.",
};

export const COMMAND_STATUS_LABELS: Record<CommandStatus, string> = {
  pending: "대기",
  running: "실행 중",
  succeeded: "성공",
  waiting_approval: "승인 대기",
  failed: "실패",
};

/** 시작·승인은 채운 버튼, 나머지는 약한 버튼. */
export const PRIMARY_ACTIONS: readonly CommandAction[] = ["run", "approve"];

export function commandTone(status: CommandStatus): Tone {
  switch (status) {
    case "succeeded":
      return "ok";
    case "running":
      return "running";
    case "waiting_approval":
      return "warn";
    case "failed":
      return "bad";
    default:
      return "idle";
  }
}

export function isActiveCommand(command: Pick<CutoverCommand, "status">) {
  return command.status === "pending" || command.status === "running";
}

export function activeCommand(commands: CutoverCommand[] | undefined) {
  return commands?.find(isActiveCommand);
}

/**
 * 실패한 단계가 되돌릴 수 없는 지점(SWITCHING) 앞인가.
 * 중단·원복(abort)은 이때만 안전하다. 실패 단계가 없거나 지점을 모르면 false.
 */
export function failedBeforeSwitch(
  stages: Pick<StageInfo, "status" | "point_of_no_return">[],
) {
  const failed = stages.findIndex((s) => s.status === "failed");
  const switching = stages.findIndex((s) => s.point_of_no_return);
  return failed >= 0 && switching >= 0 && failed < switching;
}

/** 현재 단계에서 의미 있는 명령(대기 중 명령은 고려하지 않음). */
export function visibleActions(
  stage: string | null,
  failedBeforeSwitching: boolean,
): CommandAction[] {
  const key = stageKey(stage);
  const rules: Record<CommandAction, boolean> = {
    run: !["READY", "ABORTED", "NEEDS_RECOVERY", "COMPLETED"].includes(key),
    approve: key === "READY",
    retry: key === "ABORTED",
    force_retry: key === "NEEDS_RECOVERY",
    abort: key === "ABORTED" && failedBeforeSwitching,
  };
  return COMMAND_ACTIONS.filter((action) => rules[action]);
}

/**
 * 버튼 활성 여부. 대기·실행 중 명령이 하나라도 있으면 모두 비활성(한 번에 한 명령).
 */
export function availableActions(
  stage: string | null,
  active: Pick<CutoverCommand, "status"> | undefined,
  failedBeforeSwitching = false,
): Record<CommandAction, boolean> {
  const visible = active ? [] : visibleActions(stage, failedBeforeSwitching);
  return Object.fromEntries(
    COMMAND_ACTIONS.map((action) => [action, visible.includes(action)]),
  ) as Record<CommandAction, boolean>;
}

export function commandResultText(
  command: Pick<CutoverCommand, "status" | "exit_code" | "stage">,
) {
  if (isActiveCommand(command)) return "-";
  const parts = [`exit ${command.exit_code ?? "-"}`];
  if (command.stage) parts.push(command.stage);
  return parts.join(", ");
}
