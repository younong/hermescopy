import type { CronJob } from "./api";
import { buildCronJobPayload, cronJobFormFromJob, type CronJobFormState } from "./cron-job";
import {
  buildScheduleString,
  DEFAULT_SCHEDULE_STATE,
  parseScheduleString,
  type ScheduleBuilderState,
} from "./schedule";

export interface CronJobEditorState extends CronJobFormState {
  scheduleState: ScheduleBuilderState;
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
    scheduleState: { ...DEFAULT_SCHEDULE_STATE },
  };
}

export function editorFormFromJob(job: CronJob): CronJobEditorState {
  const form = cronJobFormFromJob(job);
  return { ...form, scheduleState: parseScheduleString(form.schedule) };
}

export function buildCronJobPayloadFromEditor(form: CronJobEditorState) {
  const { scheduleState, ...payloadForm } = form;
  return buildCronJobPayload({
    ...payloadForm,
    schedule: buildScheduleString(scheduleState),
  });
}
