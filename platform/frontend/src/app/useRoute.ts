import { useEffect, useState } from "react";
import { onRouteChange, readRoute, type Route } from "./routes";

function currentRoute() {
  return readRoute(window.location.hash);
}

export function useRoute() {
  const [route, setRoute] = useState<Route>(currentRoute);
  useEffect(() => onRouteChange(window, () => setRoute(currentRoute())), []);
  function go(hash: string) {
    window.location.hash = hash;
    setRoute(currentRoute());
    window.scrollTo(0, 0);
  }
  return { route, go };
}
