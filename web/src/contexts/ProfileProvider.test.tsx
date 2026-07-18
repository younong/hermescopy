// @vitest-environment jsdom

import { act, useEffect, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProfileKeyedRoutes } from "./ProfileKeyedRoutes";
import { ProfileProvider } from "./ProfileProvider";
import { useProfileScope } from "./useProfileScope";

const apiMocks = vi.hoisted(() => ({
  getActiveProfile: vi.fn(),
  getProfiles: vi.fn(),
  setManagementProfile: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getActiveProfile: apiMocks.getActiveProfile,
      getProfiles: apiMocks.getProfiles,
    },
    setManagementProfile: apiMocks.setManagementProfile,
  };
});

let root: Root | null = null;

beforeEach(() => {
  apiMocks.getActiveProfile.mockReset();
  apiMocks.getProfiles.mockReset();
  apiMocks.setManagementProfile.mockReset();
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("ProfileProvider initial resolution", () => {
  it("mounts routed content once under the final active profile", async () => {
    const profiles = deferred<ProfilesResponse>();
    const active = deferred<{ active: string; current: string }>();
    apiMocks.getProfiles.mockReturnValue(profiles.promise);
    apiMocks.getActiveProfile.mockReturnValue(active.promise);
    const mounts: string[] = [];
    const unmounts: string[] = [];

    renderProfileTree("/chat-gui", <MountProbe mounts={mounts} unmounts={unmounts} />);

    expect(document.querySelector("[aria-busy='true']")).not.toBeNull();
    expect(mounts).toEqual([]);

    await act(async () => {
      profiles.resolve(profileList("legacy_multi_profile"));
      await profiles.promise;
    });

    expect(apiMocks.getActiveProfile).toHaveBeenCalledOnce();
    expect(mounts).toEqual([]);

    await act(async () => {
      active.resolve({ active: "work", current: "default" });
      await active.promise;
    });

    expect(document.querySelector("[aria-busy='true']")).toBeNull();
    expect(document.querySelector("[data-profile]")?.getAttribute("data-profile")).toBe("work");
    expect(mounts).toEqual(["work"]);
    expect(unmounts).toEqual([]);
  });

  it("keeps an explicit URL profile through initial resolution", async () => {
    apiMocks.getProfiles.mockResolvedValue(profileList("legacy_multi_profile"));
    apiMocks.getActiveProfile.mockResolvedValue({ active: "other", current: "default" });

    renderProfileTree("/chat-gui?profile=work", <ProfileProbe />);
    await flushEffects();

    expect(document.querySelector("[data-profile]")?.getAttribute("data-profile")).toBe("work");
    expect(document.querySelector("[data-location]")?.getAttribute("data-location")).toBe(
      "/chat-gui?profile=work",
    );
  });

  it("normalizes stale profile URLs in owner singleton mode", async () => {
    apiMocks.getProfiles.mockResolvedValue(profileList("owner_singleton"));

    renderProfileTree("/chat-gui?profile=work", <ProfileProbe />);
    await flushEffects();

    expect(apiMocks.getActiveProfile).not.toHaveBeenCalled();
    expect(document.querySelector("[data-profile]")?.getAttribute("data-profile")).toBe("");
    expect(document.querySelector("[data-location]")?.getAttribute("data-location")).toBe(
      "/chat-gui",
    );
  });

  it("fails open when profile discovery is unavailable", async () => {
    apiMocks.getProfiles.mockRejectedValue(new Error("unavailable"));

    renderProfileTree("/chat-gui?profile=work", <ProfileProbe />);
    await flushEffects();

    expect(document.querySelector("[aria-busy='true']")).toBeNull();
    expect(document.querySelector("[data-profile]")?.getAttribute("data-profile")).toBe("work");
  });

  it("remounts routed content for an intentional profile switch", async () => {
    apiMocks.getProfiles.mockResolvedValue(profileList("legacy_multi_profile"));
    apiMocks.getActiveProfile.mockResolvedValue({ active: "default", current: "default" });
    const mounts: string[] = [];
    const unmounts: string[] = [];

    renderProfileTree("/chat-gui", <MountProbe mounts={mounts} unmounts={unmounts} />);
    await flushEffects();

    expect(mounts).toEqual([""]);
    await act(async () => {
      document.querySelector<HTMLButtonElement>("[data-switch-profile]")?.click();
    });

    expect(mounts).toEqual(["", "work"]);
    expect(unmounts).toEqual([""]);
  });
});

function renderProfileTree(initialEntry: string, child: ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <MemoryRouter initialEntries={[initialEntry]}>
        <ProfileProvider>
          <ProfileKeyedRoutes>{child}</ProfileKeyedRoutes>
        </ProfileProvider>
      </MemoryRouter>,
    );
  });
}

function ProfileProbe() {
  const { profile, setProfile } = useProfileScope();
  const location = useLocation();
  return (
    <div>
      <div data-location={`${location.pathname}${location.search}`} data-profile={profile} />
      <button data-switch-profile onClick={() => setProfile("work")} type="button" />
    </div>
  );
}

function MountProbe({ mounts, unmounts }: { mounts: string[]; unmounts: string[] }) {
  const { profile } = useProfileScope();
  const [mountedProfile] = useState(profile);
  useEffect(() => {
    mounts.push(mountedProfile);
    return () => {
      unmounts.push(mountedProfile);
    };
  }, [mountedProfile, mounts, unmounts]);
  return <ProfileProbe />;
}

async function flushEffects() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

type ProfilesResponse = ReturnType<typeof profileList>;

function profileList(managementMode: "legacy_multi_profile" | "owner_singleton") {
  return {
    management_mode: managementMode,
    profiles: [{ name: "default" }, { name: "work" }],
  };
}
