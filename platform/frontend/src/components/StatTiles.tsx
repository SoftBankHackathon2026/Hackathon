import type { ReactNode } from "react";
import type { Tone } from "../features/migrations/stages";

export type StatTile = {
  id: string;
  label: ReactNode;
  value: string;
  unit?: string;
  note?: ReactNode;
  tone?: Tone | "none";
  /** 0~1 가로 막대(용량·진행률 등). */
  meter?: { ratio: number; tone?: Tone };
  extra?: ReactNode;
  /** 0 처럼 볼 것이 없는 타일은 숫자를 흐리게. */
  dim?: boolean;
};

/**
 * 화면 맨 위 숫자 타일 줄(모든 개요 공통). 라벨 → 큰 숫자 → 근거 한 줄.
 * 문제가 있는 타일만 숫자에 색을 주고 라벨 앞에 점을 둔다. 누르면 해당 목록으로 간다.
 */
export function StatTiles({
  items,
  onSelect,
  label = "요약",
}: {
  items: StatTile[];
  onSelect?: (id: string) => void;
  label?: string;
}) {
  return (
    <ul className="att" aria-label={label}>
      {items.map((item) => {
        const body = (
          <>
            <span className="att-label">{item.label}</span>
            <span className="att-value">
              {item.value}
              {item.unit && <small>{item.unit}</small>}
            </span>
            {item.meter && (
              <span
                className={`att-meter tone-${item.meter.tone ?? "running"}`}
                aria-hidden="true"
              >
                <span
                  style={{
                    width: `${Math.min(1, Math.max(0, item.meter.ratio)) * 100}%`,
                  }}
                />
              </span>
            )}
            {item.note && <span className="att-note">{item.note}</span>}
            {item.extra}
          </>
        );
        const className = `att-tile tone-${item.tone ?? "none"}${item.dim ? " dim" : ""}`;
        return (
          <li key={item.id}>
            {onSelect ? (
              <button
                type="button"
                className={className}
                onClick={() => onSelect(item.id)}
              >
                {body}
              </button>
            ) : (
              <div className={className}>{body}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
