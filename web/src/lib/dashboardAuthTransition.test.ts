import { describe, expect, it } from "vitest";

import { DashboardAuthTransition } from "./dashboardAuthTransition";

describe("DashboardAuthTransition", () => {
  it("tears down A exactly once before B becomes current", () => {
    const transition = new DashboardAuthTransition();
    const closed: string[] = [];
    transition.register(() => closed.push("socket"));

    expect(transition.transition("owner-a")).toBe(true);
    expect(closed).toEqual(["socket"]);
    expect(transition.transition("owner-a")).toBe(false);
    expect(closed).toEqual(["socket"]);
    expect(transition.transition("owner-b")).toBe(true);
    expect(closed).toEqual(["socket", "socket"]);
  });

  it("tears down the current owner on logout without affecting a later owner", () => {
    const transition = new DashboardAuthTransition();
    const closed: string[] = [];
    transition.register(() => closed.push("closed"));

    transition.transition("owner-a");
    transition.reset();
    transition.transition("owner-b");

    expect(closed).toEqual(["closed", "closed", "closed"]);
  });
});
