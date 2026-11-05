import type { ReactNode } from "react";
import { barPercent } from "../lib/bar";

export function BarCell({
  value,
  max,
  tone = "primary",
  children,
}: {
  value: number;
  max: number;
  tone?: "primary" | "bad";
  children: ReactNode;
}) {
  return (
    <span className="bar-cell">
      <span className="bar-num">{children}</span>
      <span className="bar-track" aria-hidden="true">
        <span
          className={tone === "bad" ? "bar-fill bar-bad" : "bar-fill"}
          style={{ width: `${barPercent(value, max)}%` }}
        />
      </span>
    </span>
  );
}
