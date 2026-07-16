export type DashboardAuthTeardown = () => void;

/**
 * Coordinates local dashboard resources across authenticated owner changes.
 * Owner keys namespace browser storage only; server-side authorization remains
 * entirely cookie/ticket based.
 */
export class DashboardAuthTransition {
  private ownerKey: string | undefined;
  private teardownCallbacks = new Set<DashboardAuthTeardown>();

  register(teardown: DashboardAuthTeardown): () => void {
    this.teardownCallbacks.add(teardown);
    return () => this.teardownCallbacks.delete(teardown);
  }

  transition(nextOwnerKey?: string): boolean {
    const next = nextOwnerKey?.trim() || undefined;
    if (next === this.ownerKey) return false;

    for (const teardown of this.teardownCallbacks) teardown();
    this.ownerKey = next;
    return true;
  }

  reset(): void {
    this.transition(undefined);
  }
}

export const dashboardAuthTransition = new DashboardAuthTransition();
