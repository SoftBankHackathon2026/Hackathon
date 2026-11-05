import { useState } from "react";

export function useToggleSet() {
  const [open, setOpen] = useState<string[]>([]);
  return {
    isOpen: (key: string) => open.includes(key),
    toggle: (key: string) =>
      setOpen((prev) =>
        prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
      ),
  };
}
