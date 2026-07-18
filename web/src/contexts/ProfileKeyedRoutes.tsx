import type { ReactNode } from "react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

import { useProfileScope } from "@/contexts/useProfileScope";

/**
 * Delays profile-scoped pages until their initial scope is final, then remounts
 * them whenever the operator intentionally changes profiles.
 */
export function ProfileKeyedRoutes({ children }: { children: ReactNode }) {
  const { profile, ready } = useProfileScope();
  if (!ready) {
    return (
      <div
        aria-busy="true"
        aria-live="polite"
        className="flex min-h-0 min-w-0 flex-1 items-center justify-center"
      >
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner />
          <span>Loading profile…</span>
        </div>
      </div>
    );
  }
  return <div key={profile || "__own__"} className="contents">{children}</div>;
}
