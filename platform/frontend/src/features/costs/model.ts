import type { CostBasis, CostLine } from "../../api/costs";

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

export function usd(value: number | null | undefined) {
  return value === null || value === undefined ? "-" : USD.format(value);
}

const UNIT_USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 4,
});

// GiB 단가처럼 1달러 미만인 단가는 소수 넷째 자리까지 보인다.
export function unitUsd(value: number | null | undefined) {
  if (value === null || value === undefined) return "-";
  return value < 1 ? UNIT_USD.format(value) : USD.format(value);
}

export function signedUsd(value: number | null | undefined) {
  if (value === null || value === undefined) return "-";
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${USD.format(Math.abs(value))}`;
}

export const CATEGORY_LABELS: Record<string, string> = {
  compute: "컴퓨트",
  control_plane: "컨트롤 플레인",
  storage: "스토리지",
  database: "데이터베이스",
  messaging: "메시징",
  network: "네트워크",
  transfer: "데이터 전송",
  onprem: "온프렘",
};

export function percent(part: number | null, whole: number | null) {
  if (part === null || whole === null || whole <= 0) return null;
  return Math.round((part / whole) * 100);
}

export type CategoryShare = { category: string; monthly: number };

export function topLines(lines: CostLine[], count: number) {
  return [...lines]
    .filter((line) => (line.monthly ?? 0) > 0)
    .sort((a, b) => (b.monthly ?? 0) - (a.monthly ?? 0))
    .slice(0, count);
}

export const BASIS_LABELS: Record<CostBasis, string> = {
  list_price: "공식 단가",
  owner_input: "운영자 입력",
  measured: "측정값",
  not_priced: "가격 정보 없음",
};

export function quantityText(line: CostLine) {
  const amount = Number.isInteger(line.quantity)
    ? String(line.quantity)
    : line.quantity.toFixed(2);
  return `${amount} ${line.unit}`;
}

export function byCategory(lines: CostLine[]) {
  const totals = new Map<string, number>();
  for (const line of lines)
    totals.set(
      line.category,
      (totals.get(line.category) ?? 0) + (line.monthly ?? 0),
    );
  return [...totals.entries()]
    .map(([category, monthly]) => ({ category, monthly }))
    .sort((a, b) => b.monthly - a.monthly);
}
