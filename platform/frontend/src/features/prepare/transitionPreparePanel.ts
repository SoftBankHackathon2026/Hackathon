import { flushSync } from "react-dom";

export function transitionPreparePanel(update: () => void) {
  if (!document.startViewTransition) {
    update();
    return;
  }

  const transition = document.startViewTransition(() => flushSync(update));
  // Starting a newer transition can skip the previous snapshot.
  void transition.ready.catch(() => {});
}
