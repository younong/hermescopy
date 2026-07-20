import {
  createContext,
  createElement,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { api, type AuthMeResponse } from "@/lib/api";
import { getHermesBrowserId } from "@/lib/browserIdentity";
import { dashboardAuthTransition } from "@/lib/dashboardAuthTransition";

export interface DashboardAuthIdentity {
  authMe: AuthMeResponse | null;
  authRequired: boolean;
  error: string | null;
  loading: boolean;
  ownerKey?: string;
  ready: boolean;
  refresh: () => Promise<void>;
}

function isAuthRequired(): boolean {
  return typeof window !== "undefined" && !!window.__HERMES_AUTH_REQUIRED__;
}

const DashboardAuthIdentityContext = createContext<DashboardAuthIdentity | null>(null);

export function DashboardAuthIdentityProvider({ children }: { children: ReactNode }) {
  const identity = useDashboardAuthIdentityState();
  return createElement(DashboardAuthIdentityContext.Provider, { value: identity }, children);
}

export function useDashboardAuthIdentity(): DashboardAuthIdentity {
  const identity = useContext(DashboardAuthIdentityContext);
  if (!identity) {
    throw new Error("useDashboardAuthIdentity must be used within DashboardAuthIdentityProvider");
  }
  return identity;
}

function useDashboardAuthIdentityState(): DashboardAuthIdentity {
  const [authRequired] = useState(isAuthRequired);
  const [authMe, setAuthMe] = useState<AuthMeResponse | null>(null);
  const [loading, setLoading] = useState(authRequired);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  const refresh = useMemo(
    () => async () => {
      if (!authRequired) {
        if (mounted.current) {
          dashboardAuthTransition.transition(undefined);
          setAuthMe(null);
          setLoading(false);
          setError(null);
        }
        return;
      }

      if (mounted.current) {
        setLoading(true);
        setError(null);
      }
      try {
        const identity = await api.getAuthMe();
        if (mounted.current) {
          dashboardAuthTransition.transition(identity.owner_key);
          setAuthMe(identity);
        }
      } catch (err) {
        if (mounted.current) {
          setAuthMe(null);
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (mounted.current) setLoading(false);
      }
    },
    [authRequired],
  );

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
    };
  }, [refresh, mounted]);

  const ownerKey = authMe?.owner_key;

  return {
    authMe,
    authRequired,
    error,
    loading,
    ownerKey,
    ready: !authRequired || !!ownerKey,
    refresh,
  };
}

export function useDashboardBrowserIdentity(): DashboardAuthIdentity & { browserId: string } {
  const identity = useDashboardAuthIdentity();
  const browserId = useMemo(
    () => (identity.ready ? getHermesBrowserId(identity.ownerKey) : ""),
    [identity.ownerKey, identity.ready],
  );

  return { ...identity, browserId };
}
