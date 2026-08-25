import type { CronJob } from "./api";
import { buildCronJobPayload, cronJobFormFromJob, type CronJobFormState } from "./cron-job";
import {
  buildScheduleString,
  DEFAULT_SCHEDULE_STATE,
  parseScheduleString,
  type ScheduleBuilderState,
} from "./schedule";

export type CronJobEditorMode = "employee" | "custom";

export interface CronJobEditorState extends CronJobFormState {
  scheduleState: ScheduleBuilderState;
  /** 二选一: the job either runs as a pinned AI employee (`employee`) or with
   * manually picked model/skills/advanced options (`custom`). */
  mode: CronJobEditorMode;
}

export function emptyCronJobForm(): CronJobEditorState {
  return {
    name: "",
    prompt: "",
    schedule: "",
    deliver: "local",
    skills: [],
    provider: "",
    model: "",
    base_url: "",
    script: "",
    no_agent: false,
    context_from: "",
    enabled_toolsets: [],
    workdir: "",
    employee_id: "",
    target_employee_ids: [],
    mode: "employee",
    scheduleState: { ...DEFAULT_SCHEDULE_STATE },
  };
}

export function editorFormFromJob(job: CronJob): CronJobEditorState {
  const form = cronJobFormFromJob(job);
  return {
    ...form,
    mode: form.employee_id || form.target_employee_ids.length ? "employee" : "custom",
    scheduleState: parseScheduleString(form.schedule),
  };
}

export function buildCronJobPayloadFromEditor(form: CronJobEditorState) {
  const { scheduleState, mode, ...payloadForm } = form;
  const payload = buildCronJobPayload({
    ...payloadForm,
    schedule: buildScheduleString(scheduleState),
  });
  if (mode === "employee") {
    // Employee jobs run under the selected employee policies resolved at fire
    // time; clear the mutually-exclusive single target when fan-out targets
    // are selected, and clear every manual execution knob.
    return {
      ...payload,
      employee_id: payload.target_employee_ids?.length ? null : payload.employee_id,
      skills: [],
      provider: null,
      model: null,
      base_url: null,
      script: null,
      no_agent: false,
      context_from: null,
      enabled_toolsets: null,
      workdir: null,
    };
  }
  return { ...payload, employee_id: null, target_employee_ids: null };
}
