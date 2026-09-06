import { useCallback, useEffect, useState } from "react";

export interface LocationState {
  pathname: string;
  search: URLSearchParams;
}

interface NavigateOptions {
  replace?: boolean;
}

function currentLocation(): LocationState {
  return {
    pathname: window.location.pathname,
    search: new URLSearchParams(window.location.search),
  };
}

export function useLocationState(): [
  LocationState,
  (path: string, options?: NavigateOptions) => void,
] {
  const [location, setLocation] = useState<LocationState>(currentLocation);

  useEffect(() => {
    const onPopState = () => setLocation(currentLocation());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const navigate = useCallback(
    (path: string, options?: NavigateOptions) => {
      const method = options?.replace ? "replaceState" : "pushState";
      window.history[method](null, "", path);
      setLocation(currentLocation());
      window.scrollTo({ top: 0, behavior: "auto" });
    },
    [],
  );

  return [location, navigate];
}

export function withSearch(
  pathname: string,
  current: URLSearchParams,
  updates: Record<string, string | null>,
): string {
  const next = new URLSearchParams(current);
  Object.entries(updates).forEach(([key, value]) => {
    if (value) next.set(key, value);
    else next.delete(key);
  });
  const encoded = next.toString();
  return encoded ? `${pathname}?${encoded}` : pathname;
}

export function globalNavigationPath(pathname: string, user: string): string {
  return withSearch(pathname, new URLSearchParams(), { user });
}