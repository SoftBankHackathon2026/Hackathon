import { Children, type CSSProperties, type ReactNode } from "react";
import type { Tone } from "../features/migrations/stages";
import { StatusDot } from "./StatusDot";

export type CardLine = { tone: Tone | "none"; text: string };

export function SignalCard({
  title,
  line,
  note,
}: {
  title: string;
  line: CardLine;
  note?: string;
}) {
  return (
    <div className={`signal-card ${line.tone === "bad" ? "signal-bad" : ""}`}>
      <h3>{title}</h3>
      <div className="signal-line">
        {line.tone === "none" ? (
          <span className="signal-text">{line.text}</span>
        ) : (
          <StatusDot tone={line.tone}>
            <span className="signal-text">{line.text}</span>
          </StatusDot>
        )}
      </div>
      {note && <p className="signal-note">{note}</p>}
    </div>
  );
}

export function SignalRow({ children }: { children: ReactNode }) {
  const count = Children.toArray(children).length;
  return (
    <div
      className="signal-row"
      data-n={count}
      style={{ "--n": count } as CSSProperties}
    >
      {children}
    </div>
  );
}
