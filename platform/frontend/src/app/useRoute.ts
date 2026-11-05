import { useEffect, useState } from "react";
import { readRoute, type Route } from "./routes";

function currentRoute() {
  return readRoute(window.location.hash);
}

export function useRoute() {
  const [route, setRoute] = useState<Route>(currentRoute);
  useEffect(() => {
    const changed = () => setRoute(currentRoute());
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  function go(hash: string) {
    window.location.hash = hash;
    setRoute(currentRoute());
    window.scrollTo(0, 0);
  }
  return { route, go };
}
