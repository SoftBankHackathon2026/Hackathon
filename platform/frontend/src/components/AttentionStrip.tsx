import type { Tone } from "../features/migrations/stages";
import { StatTiles } from "./StatTiles";

/** 먼저 볼 것: 개수 타일 줄. 0 이면 회색, 문제가 있으면 그 색(StatTiles 와 같은 모양). */
export function AttentionStrip({
  items,
  onSelect,
}: {
  items: {
    id: string;
    value: number;
    label: string;
    note: string;
    tone: Tone | "none";
  }[];
  onSelect?: (id: string) => void;
}) {
  return (
    <StatTiles
      label="먼저 볼 것"
      onSelect={onSelect}
      items={items.map((item) => ({
        id: item.id,
        label: item.label,
        value: item.value.toLocaleString("ko-KR"),
        note: item.note,
        tone: item.value > 0 ? item.tone : "none",
        dim: item.value === 0,
      }))}
    />
  );
}
