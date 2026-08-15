// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import type { SessionCompositionResponse } from "@/lib/api";
import { SessionCompositionCharts } from "../SessionCompositionCharts";

function payload(
  overrides: Partial<SessionCompositionResponse["charts"][number]> = {},
): SessionCompositionResponse {
  return {
    scope: {
      requested_ids: ["session-a"],
      canonical_session_count: 1,
      canonical_root_ids: ["session-a"],
      canonical_tip_ids: ["session-a"],
      aggregation: "full_compression_lineage",
      date_truncation: false,
    },
    coverage: {},
    limitations: [],
    charts: [
      {
        id: "database_messages",
        label: "Database messages",
        availability: "partial",
        accuracy: "exact_count",
        unit: "messages",
        total: null,
        known_total: 2,
        coverage: { requested_sessions: 1, included_sessions: 1 },
        limitations: [{ code: "historical_tool_schemas_unavailable" }],
        segments: [
          {
            id: "known-zero",
            label: "Known zero",
            value: 0,
            percentage: 0,
            unit: "messages",
            status: "exact",
          },
          {
            id: "unknown",
            label: "Unknown role",
            value: null,
            percentage: null,
            unit: "messages",
            status: "unavailable",
          },
          {
            id: "user",
            label: "User",
            value: 2,
            percentage: 100,
            unit: "messages",
            status: "exact",
          },
        ],
        ...overrides,
      },
    ],
  };
}

beforeEach(() => {
  (
    globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("SessionCompositionCharts", () => {
  it("keeps unknown distinct from zero and exposes accessible chart text", async () => {
    vi.spyOn(api, "getSessionComposition").mockResolvedValue(payload());
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(<SessionCompositionCharts ids={["session-a"]} />);
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Known zero0 messages");
    expect(container.textContent).toContain("Unknown roleUnavailable");
    expect(container.textContent).toContain(
      "Percentages are of the known total",
    );
    expect(container.textContent).toContain(
      "historical tool schemas unavailable",
    );
    const image = container.querySelector('[role="img"]');
    expect(image?.getAttribute("aria-label")).toContain(
      "Known zero: 0 messages",
    );
    expect(image?.getAttribute("aria-label")).toContain(
      "Unknown role: Unavailable",
    );
    expect(container.querySelectorAll("svg circle")).toHaveLength(2);
    await act(async () => root.unmount());
  });

  it("sorts IDs, aborts stale requests, and renders unavailable charts", async () => {
    const signals: AbortSignal[] = [];
    vi.spyOn(api, "getSessionComposition").mockImplementation(
      (_ids, options = {}) => {
        signals.push(options.signal!);
        return Promise.resolve(
          payload({
            availability: "unavailable",
            total: null,
            known_total: 0,
          }),
        );
      },
    );
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(<SessionCompositionCharts ids={["z", "a"]} />);
      await Promise.resolve();
    });
    expect(api.getSessionComposition).toHaveBeenLastCalledWith(
      ["a", "z"],
      expect.any(Object),
    );
    await act(async () => {
      root.render(<SessionCompositionCharts ids={["b"]} />);
      await Promise.resolve();
    });
    expect(signals[0].aborted).toBe(true);
    expect(container.textContent).toContain("Unavailable");
    await act(async () => root.unmount());
  });
});
