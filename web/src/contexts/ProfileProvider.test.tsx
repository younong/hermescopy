// @vitest-environment jsdom

import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, getManagementProfile, setManagementProfile } from "@/lib/api";
import { ProfileProvider } from "./ProfileProvider";
import { useProfileScope } from "./useProfileScope";

let root: Root | null = null;
let latestScope: ReturnType<typeof useProfileScope> | null = null;
let latestSearch = "";

function Probe() {
  const scope = useProfileScope();
  const location = useLocation();
  useEffect(() => {
    latestScope = scope;
    latestSearch = location.search;
  }, [location.search, scope]);
  return null;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
  latestScope = null;
  latestSearch = "";
  setManagementProfile("");
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("ProfileProvider", () => {
  it("boots legacy profile scope from one summary request", async () => {
    const summary = vi.spyOn(api, "getProfilesSummary").mockResolvedValue({
      management_mode: "legacy_multi_profile",
      profiles: ["default", "coder"],
      current: "default",
      active: "coder",
    });
    const rich = vi.spyOn(api, "getProfiles");
    const active = vi.spyOn(api, "getActiveProfile");

    await renderProvider("/skills");

    expect(summary).toHaveBeenCalledOnce();
    expect(rich).not.toHaveBeenCalled();
    expect(active).not.toHaveBeenCalled();
    expect(latestScope).toMatchObject({
      profile: "coder",
      currentProfile: "default",
      profiles: ["default", "coder"],
      managementMode: "legacy_multi_profile",
    });
    expect(latestSearch).toBe("?profile=coder");
    expect(getManagementProfile()).toBe("coder");
  });

  it("gives an explicit legacy profile query precedence over sticky active", async () => {
    vi.spyOn(api, "getProfilesSummary").mockResolvedValue({
      management_mode: "legacy_multi_profile",
      profiles: ["default", "coder", "research"],
      current: "default",
      active: "coder",
    });

    await renderProvider("/skills?profile=research");

    expect(latestScope?.profile).toBe("research");
    expect(latestSearch).toBe("?profile=research");
  });

  it("clears profile selection in owner-singleton mode", async () => {
    vi.spyOn(api, "getProfilesSummary").mockResolvedValue({
      management_mode: "owner_singleton",
      profiles: ["default"],
      current: "default",
      active: "default",
    });

    await renderProvider("/skills?profile=host-profile");

    expect(latestScope).toMatchObject({
      profile: "",
      currentProfile: "default",
      profiles: ["default"],
      managementMode: "owner_singleton",
    });
    expect(latestSearch).toBe("");
    expect(getManagementProfile()).toBe("");
  });
});

async function renderProvider(entry: string) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <MemoryRouter initialEntries={[entry]}>
        <ProfileProvider>
          <Probe />
        </ProfileProvider>
      </MemoryRouter>,
    );
  });
  await act(async () => {
    await Promise.resolve();
  });
}
