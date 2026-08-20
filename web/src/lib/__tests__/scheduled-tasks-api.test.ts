import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("scheduled tasks API", () => {
  it("routes Chat GUI task calls through the scheduled-tasks plugin", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve([]),
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.getCronJobs();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/plugins/scheduled-tasks/jobs",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("wraps scheduled-task updates in the plugin API update envelope", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ id: "job-1" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.updateCronJob("job-1", { name: "Renamed" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/plugins/scheduled-tasks/jobs/job-1",
      expect.objectContaining({
        body: JSON.stringify({ updates: { name: "Renamed" } }),
        credentials: "include",
        method: "PUT",
      }),
    );
  });
});
