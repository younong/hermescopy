import { useEffect, useMemo, useState } from "react";

import { api, type AuthMeResponse } from "@/lib/api";
import { getHermesBrowserId } from "@/lib/browserIdentity";

interface DashboardAuthIdentity {
  authMe: AuthMeResponse | null;
  authRequired: boolean;
  error: string | null;
  loading: boolean;
  ownerKey?: string;
  ready: boolean;
}

function isAuthRequired(): boolean {
  return typeof window !== "undefined" && !!window.__HERMES_AUTH_REQUIRED__;
}

export function useDashboardAuthIdentity(): DashboardAuthIdentity {
  const [authRequired] = useState(isAuthRequired);
  const [authMe, setAuthMe] = useState<AuthMeResponse | null>(null);
  const [loading, setLoading] = useState(authRequired);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!authRequired) {
      setAuthMe(null);
      setLoading(false);
      setError(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getAuthMe()
      .then((data) => {
        if (cancelled) return;
        setAuthMe(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setAuthMe(null);
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [authRequired]);

  const ownerKey = authMe?.owner_key;
  return {
    authMe,
    authRequired,
    error,
    loading,
    ownerKey,
    ready: !authRequired || !!ownerKey,
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
