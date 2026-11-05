import { createContext, useContext, type ReactNode } from "react";
import { createPortal } from "react-dom";

// The page header belongs to the shell, but its tabs and buttons belong to the
// page. Pages hand them over through these slots.
export type Slots = { tabs: HTMLElement | null; actions: HTMLElement | null };
export const SlotContext = createContext<Slots>({ tabs: null, actions: null });

export function PageTabs({ children }: { children: ReactNode }) {
  const { tabs } = useContext(SlotContext);
  return tabs ? createPortal(children, tabs) : null;
}

export function PageActions({ children }: { children: ReactNode }) {
  const { actions } = useContext(SlotContext);
  return actions ? createPortal(children, actions) : null;
}
