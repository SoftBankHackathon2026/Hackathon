import { useEffect, useState } from "react";
import { ROUTE_EVENT, readRoute, type Route } from "./routes";

function currentRoute() {
  return readRoute(window.location.hash);
}

export function useRoute() {
  const [route, setRoute] = useState<Route>(currentRoute);
  useEffect(() => {
    const changed = () => setRoute(currentRoute());
    window.addEventListener("hashchange", changed);
    window.addEventListener(ROUTE_EVENT, changed);
    return () => {
      window.removeEventListener("hashchange", changed);
      window.removeEventListener(ROUTE_EVENT, changed);
    };
  }, []);
  function go(hash: string) {
    window.location.hash = hash;
    setRoute(currentRoute());
    window.scrollTo(0, 0);
  }
  return { route, go };
}
