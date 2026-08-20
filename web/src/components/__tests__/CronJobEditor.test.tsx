// @vitest-environment jsdom

import { describe, expect, it } from "vitest";
import type { CronJob } from "@/lib/api";
import {
  buildCronJobPayloadFromEditor,
  editorFormFromJob,
  emptyCronJobForm,
} from "@/lib/cron-job-editor";

describe("CronJobEditor helpers", () => {
  it("round-trips the scheduled task page job shape through the shared schedule editor", () => {
    const job: CronJob = {
      id: "weekly",
      name: "Weekly summary",
      prompt: "Summarize the week",
      schedule: { kind: "cron", expr: "30 14 * * 1,3,5" },
      enabled: true,
      deliver: "local",
      skills: ["release-notes"],
    };

    const form = editorFormFromJob(job);
    expect(form.scheduleState).toMatchObject({
      mode: "weekly",
      timeOfDay: "14:30",
      weekdays: [1, 3, 5],
    });
    expect(buildCronJobPayloadFromEditor(form)).toMatchObject({
      name: "Weekly summary",
      prompt: "Summarize the week",
      schedule: "30 14 * * 1,3,5",
      skills: ["release-notes"],
    });
  });

  it("keeps the existing interval default for new scheduled task page and Chat GUI forms", () => {
    const form = emptyCronJobForm();
    expect(form.scheduleState.mode).toBe("interval");
    expect(buildCronJobPayloadFromEditor(form).schedule).toBe("every 30m");
  });
});
