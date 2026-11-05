import type { ReactNode } from "react";
import type { Tone } from "../features/migrations/stages";

export function StatusDot({
  tone,
  children,
}: {
  tone: Tone;
  children: ReactNode;
}) {
  return (
    <span className={`status tone-${tone}`}>
      <i className={`dot dot-${tone}`} aria-hidden="true" />
      {children}
    </span>
  );
}

export function StateLabel({
  tone,
  children,
}: {
  tone: Tone;
  children: ReactNode;
}) {
  return <StatusDot tone={tone}>{children}</StatusDot>;
}
