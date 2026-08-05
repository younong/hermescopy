// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CronJob } from "@/lib/api";
import { GuiChatScheduledTasksPane } from "./GuiChatScheduledTasksPane";

const mocks = vi.hoisted(() => ({
  createCronJob: vi.fn(),
  deleteCronJob: vi.fn(),
  getCronDeliveryTargets: vi.fn(),
  getCronJobs: vi.fn(),
  getModelOptions: vi.fn(),
  getSkills: vi.fn(),
  getToolsets: vi.fn(),
  pauseCronJob: vi.fn(),
  resumeCronJob: vi.fn(),
  updateCronJob: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, ...mocks } };
});

vi.mock("@/i18n", () => ({
  useI18n: () => ({
    t: {
      cron: {
        nameOptional: "Name (optional)",
        namePlaceholder: "Name",
        prompt: "Prompt",
        promptPlaceholder: "What should Hermes do?",
        deliverTo: "Deliver to",
        delivery: { local: "Local" },
        scheduleMode: "Schedule",
        scheduleModes: {
          interval: "Interval", daily: "Daily", weekly: "Weekly", monthly: "Monthly", once: "Once", custom: "Custom",
          intervalEvery: "Every", intervalUnit: "Unit", unitMinutes: "Minutes", unitHours: "Hours", unitDays: "Days",
          timeOfDay: "Time", weekdays: "Weekdays", weekdaysShort: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
          dayOfMonth: "Day", onceAt: "At", customLabel: "Expression", customPlaceholder: "0 9 * * *", customHint: "Cron expression",
          preview: "Preview", previewEmpty: "Choose a schedule",
        },
      },
    },
  }),
}));

const jobs: CronJob[] = [
  {
    id: "daily-brief",
    name: "Daily brief",
    prompt: "Summarize project updates",
    schedule: { kind: "cron", expr: "0 9 * * *" },
    enabled: true,
    state: "scheduled",
    next_run_at: "2026-08-01T09:00:00Z",
  },
  {
    id: "weekly-report",
    name: "Weekly report",
    prompt: "Prepare operations report",
    schedule: { kind: "cron", expr: "0 17 * * 5" },
    enabled: false,
    state: "paused",
  },
];

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.getCronJobs.mockResolvedValue(jobs);
  mocks.getCronDeliveryTargets.mockResolvedValue({ targets: [] });
  mocks.getSkills.mockResolvedValue([]);
  mocks.getToolsets.mockResolvedValue([]);
  mocks.getModelOptions.mockResolvedValue(null);
  mocks.pauseCronJob.mockResolvedValue({ ...jobs[0], enabled: false, state: "paused" });
  mocks.resumeCronJob.mockResolvedValue({ ...jobs[1], enabled: true, state: "scheduled" });
  mocks.updateCronJob.mockResolvedValue({ ...jobs[0], name: "Updated brief" });
  mocks.createCronJob.mockResolvedValue({ ...jobs[0], id: "new-task", name: "New task" });
  mocks.deleteCronJob.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("GuiChatScheduledTasksPane", () => {
  it("loads for the current owner, searches status, and pauses a task", async () => {
    const container = await renderPane();
    expect(mocks.getCronJobs).toHaveBeenCalledWith();
    expect(container.textContent).toContain("Daily brief");
    expect(container.textContent).toContain("Weekly report");

    changeValue(container.querySelector('[aria-label="Search scheduled tasks"]'), "paused");
    expect(container.textContent).not.toContain("Daily brief");
    expect(container.textContent).toContain("Weekly report");

    changeValue(container.querySelector('[aria-label="Search scheduled tasks"]'), "Daily");
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[aria-label="Pause Daily brief"]')?.click();
      await Promise.resolve();
    });
    expect(mocks.pauseCronJob).toHaveBeenCalledWith("daily-brief");
    expect(container.textContent).toContain("paused");
  });

  it("edits with the shared cron editor and preserves owner-scoped API calls", async () => {
    const container = await renderPane();
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Edit Daily brief"]')?.click());
    await flush();

    expect(mocks.getSkills).toHaveBeenCalledWith();
    expect(document.body.querySelector('#chat-cron-edit-prompt')).not.toBeNull();
    changeValue(document.body.querySelector('#chat-cron-edit-name'), "Updated brief");
    await act(async () => {
      buttonNamed(document.body, "Save changes")?.click();
      await Promise.resolve();
    });

    expect(mocks.updateCronJob).toHaveBeenCalledWith(
      "daily-brief",
      expect.objectContaining({ name: "Updated brief", schedule: "0 9 * * *" }),
    );
    expect(container.textContent).toContain("Updated brief");
  });

  it("confirms deletion and keeps request failures visible", async () => {
    mocks.deleteCronJob.mockRejectedValue(new Error("Task is currently running"));
    const container = await renderPane();
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Delete Daily brief"]')?.click());
    await act(async () => {
      buttonNamed(document.body, "Delete")?.click();
      await Promise.resolve();
    });

    expect(mocks.deleteCronJob).toHaveBeenCalledWith("daily-brief");
    expect(container.textContent).toContain("Task is currently running");
    expect(container.textContent).toContain("Daily brief");
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull();
  });
});

async function renderPane() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<GuiChatScheduledTasksPane />);
    await Promise.resolve();
  });
  return container;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function changeValue(element: Element | null, value: string) {
  if (!element) throw new Error("Expected form control");
  const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  act(() => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function buttonNamed(rootNode: ParentNode, text: string) {
  return Array.from(rootNode.querySelectorAll<HTMLButtonElement>("button"))
    .find((button) => button.textContent?.trim() === text);
}
