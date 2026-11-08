import type { Tone } from "../migrations/stages.ts";
import { type ChangeRow, changeRows } from "./model.ts";

export type HypothesisStatus = "validated" | "inconclusive" | "invalidated";

export type Hypothesis = {
  id: string;
  title: string;
  key: string | null;
  status: HypothesisStatus;
  evidence: string;
  fixed: boolean;
};

export const HYPOTHESIS_LABELS: Record<HypothesisStatus, string> = {
  validated: "원인",
  inconclusive: "확인 못 함",
  invalidated: "아님",
};

const HYPOTHESIS_TONES: Record<HypothesisStatus, Tone> = {
  validated: "bad",
  inconclusive: "idle",
  invalidated: "idle",
};

const ORDER: Record<HypothesisStatus, number> = {
  validated: 0,
  inconclusive: 1,
  invalidated: 2,
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown) {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function isStatus(value: unknown): value is HypothesisStatus {
  return (
    value === "validated" || value === "inconclusive" || value === "invalidated"
  );
}

export function hypothesisTone(status: HypothesisStatus): Tone {
  return HYPOTHESIS_TONES[status];
}

export function hypotheses(detail: Record<string, unknown>): Hypothesis[] {
  const raw = detail.hypotheses;
  if (!Array.isArray(raw)) return [];
  const changed = new Set(changeRows(detail).map((change) => change.key));
  return raw
    .flatMap((item) => {
      const h = record(item);
      const id = text(h.id);
      const title = text(h.title);
      if (id === null || title === null || !isStatus(h.status)) return [];
      const key = text(h.key);
      return [
        {
          id,
          title,
          key,
          status: h.status,
          evidence: text(h.evidence) ?? "",
          fixed: key !== null && h.status === "validated" && changed.has(key),
        },
      ];
    })
    .sort((a, b) => ORDER[a.status] - ORDER[b.status]);
}

export type SandboxStepKind = "say" | "think" | "run" | "edit";

export type SandboxStep = {
  kind: SandboxStepKind;
  text: string;
  ok: boolean | null;
};

export type SandboxView = {
  runner: string | null;
  image: string | null;
  model: string | null;
  seconds: number | null;
  steps: SandboxStep[];
  file: { path: string; before: string; after: string | null } | null;
};

export const SANDBOX_STEP_LABELS: Record<SandboxStepKind, string> = {
  say: "설명",
  think: "생각",
  run: "명령",
  edit: "파일 수정",
};

function isKind(value: unknown): value is SandboxStepKind {
  return (
    value === "say" || value === "think" || value === "run" || value === "edit"
  );
}

export function sandbox(detail: Record<string, unknown>): SandboxView | null {
  const raw = detail.sandbox;
  if (raw === null || typeof raw !== "object" || Array.isArray(raw))
    return null;
  const box = record(raw);
  const steps = Array.isArray(box.steps)
    ? box.steps.flatMap((item) => {
        const s = record(item);
        const body = text(s.text);
        if (!isKind(s.kind) || body === null) return [];
        return [
          {
            kind: s.kind,
            text: body,
            ok: typeof s.ok === "boolean" ? s.ok : null,
          },
        ];
      })
    : [];
  const file = record(box.file);
  const before = typeof file.before === "string" ? file.before : null;
  return {
    runner: text(box.runner),
    image: text(box.image),
    model: text(box.model),
    seconds: typeof box.seconds === "number" ? box.seconds : null,
    steps,
    file:
      before === null
        ? null
        : {
            path: text(file.path) ?? "config/target.json",
            before,
            after: typeof file.after === "string" ? file.after : null,
          },
  };
}

export type DiffLine = { kind: "same" | "del" | "add"; text: string };

function lines(value: string) {
  const out = value.split("\n");
  if (out.at(-1) === "") out.pop();
  return out;
}

export function diffLines(before: string, after: string): DiffLine[] {
  const a = lines(before);
  const b = lines(after);
  const table = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i -= 1)
    for (let j = b.length - 1; j >= 0; j -= 1)
      table[i][j] =
        a[i] === b[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1]);
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i] });
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      out.push({ kind: "del", text: a[i] });
      i += 1;
    } else {
      out.push({ kind: "add", text: b[j] });
      j += 1;
    }
  }
  for (; i < a.length; i += 1) out.push({ kind: "del", text: a[i] });
  for (; j < b.length; j += 1) out.push({ kind: "add", text: b[j] });
  return out;
}

export type FixView = {
  cause: string | null;
  impact: string | null;
  checked: string | null;
  changes: ChangeRow[];
  diff: DiffLine[] | null;
  path: string | null;
};

export function fixView(detail: Record<string, unknown>): FixView | null {
  const proposal = record(detail.proposal);
  const changes = changeRows(detail);
  const file = sandbox(detail)?.file ?? null;
  const diff =
    file !== null && file.after !== null
      ? diffLines(file.before, file.after)
      : null;
  if (changes.length === 0 && diff === null) return null;
  return {
    cause: text(proposal.cause),
    impact: text(proposal.impact),
    checked: text(proposal.verification),
    changes,
    diff:
      diff !== null && diff.some((line) => line.kind !== "same") ? diff : null,
    path: file?.path ?? null,
  };
}
