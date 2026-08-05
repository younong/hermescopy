import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CalendarClock,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-react";
import {
  CronJobFormFields,
  type CronJobFormResources,
} from "@/components/CronJobEditor";
import { api, type CronJob } from "@/lib/api";
import {
  buildCronJobPayloadFromEditor,
  editorFormFromJob,
  emptyCronJobForm,
  type CronJobEditorState,
} from "@/lib/cron-job-editor";
import { cronJobHasExecutionContent } from "@/lib/cron-job";
import { describeSchedule, englishOrdinal } from "@/lib/schedule";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";

const LOCAL_DELIVERY = {
  id: "local",
  name: "Local",
  home_target_set: true,
  home_env_var: null,
};
const EMPTY_RESOURCES: CronJobFormResources = {
  availableSkills: [],
  availableToolsets: [],
  modelOptions: null,
  deliveryTargets: [LOCAL_DELIVERY],
};
const ENGLISH_SCHEDULE_STRINGS = {
  none: "No schedule",
  everyMinutes: "Every {n} minute(s)",
  everyHours: "Every {n} hour(s)",
  everyDays: "Every {n} day(s)",
  dailyAt: "Daily at {time}",
  weeklyAt: "Weekly on {days} at {time}",
  monthlyAt: "Monthly on the {day} at {time}",
  onceAt: "Once at {time}",
  weekdaysShort: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as [string, string, string, string, string, string, string],
  ordinal: englishOrdinal,
};

export function GuiChatScheduledTasksPane() {
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyJob, setBusyJob] = useState<string | null>(null);
  const [editor, setEditor] = useState<{ job: CronJob | null; form: CronJobEditorState } | null>(null);
  const [resources, setResources] = useState<CronJobFormResources>(EMPTY_RESOURCES);
  const [pendingDelete, setPendingDelete] = useState<CronJob | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await api.getCronJobs();
      setJobs(sortJobs(rows));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const editorOpen = editor !== null;
  useEffect(() => {
    if (!editorOpen) return;
    let cancelled = false;
    Promise.all([
      api.getSkills().catch(() => []),
      api.getToolsets().catch(() => []),
      api.getModelOptions().catch(() => null),
      api.getCronDeliveryTargets().catch(() => ({ targets: [LOCAL_DELIVERY] })),
    ]).then(([skills, toolsets, modelOptions, delivery]) => {
      if (cancelled) return;
      setResources({
        availableSkills: [...skills].sort((a, b) => a.name.localeCompare(b.name)),
        availableToolsets: [...toolsets].sort((a, b) => a.name.localeCompare(b.name)),
        modelOptions,
        deliveryTargets: delivery.targets.length ? delivery.targets : [LOCAL_DELIVERY],
      });
    });
    return () => {
      cancelled = true;
    };
  }, [editorOpen, ]);

  const visibleJobs = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return jobs;
    return (jobs).filter((job) =>
      [jobTitle(job), job.prompt, job.script, job.state, job.last_status, job.last_error]
        .some((value) => String(value || "").toLowerCase().includes(normalized)),
    );
  }, [jobs, query]);

  const pauseResume = async (job: CronJob) => {
    setBusyJob(job.id);
    setError(null);
    try {
      const updated = jobState(job) === "paused"
        ? await api.resumeCronJob(job.id, )
        : await api.pauseCronJob(job.id, );
      setJobs((current) => current.map((row) => row.id === job.id ? updated : row));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusyJob(null);
    }
  };

  const saveEditor = async () => {
    if (!editor) return;
    const payload = buildCronJobPayloadFromEditor(editor.form);
    if (!payload.schedule || (!payload.no_agent && !cronJobHasExecutionContent(payload))) {
      setError("Prompt or execution content and schedule are required.");
      return;
    }
    if (payload.no_agent && !payload.script) {
      setError("no_agent jobs require a script.");
      return;
    }

    const key = editor.job?.id ?? "__create__";
    setBusyJob(key);
    setError(null);
    try {
      if (editor.job) {
        const updated = await api.updateCronJob(editor.job.id, payload, );
        setJobs((current) => sortJobs(current.map((row) => row.id === editor.job?.id ? updated : row)));
      } else {
        const created = await api.createCronJob(payload, );
        setJobs((current) => sortJobs([...current, created]));
      }
      setEditor(null);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusyJob(null);
    }
  };

  const deleteJob = async () => {
    if (!pendingDelete) return;
    const id = pendingDelete.id;
    setBusyJob(id);
    setError(null);
    try {
      await api.deleteCronJob(id, );
      setJobs((current) => current.filter((job) => job.id !== id));
      setPendingDelete(null);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusyJob(null);
    }
  };

  return (
    <section aria-label="Scheduled Tasks" className="gui-chat-workspace-pane" data-scheduled-tasks-pane>
      <header className="gui-chat-workspace-toolbar">
        <button
          className="gui-chat-workspace-primary-button"
          onClick={() => {
            setResources(EMPTY_RESOURCES);
            setEditor({ job: null, form: emptyCronJobForm() });
          }}
          type="button"
        >
          <Plus aria-hidden />
          New task
        </button>
        <button
          aria-label="Refresh scheduled tasks"
          className="gui-chat-workspace-icon-button"
          disabled={loading}
          onClick={() => void load()}
          type="button"
        >
          <RefreshCw aria-hidden className={loading ? "animate-spin" : ""} />
        </button>
      </header>

      <div className="gui-chat-workspace-heading">
        <div>
          <h1>Scheduled Tasks</h1>
          <p>Automations that run in this workspace on a recurring or one-time schedule.</p>
        </div>
        <label className="gui-chat-workspace-search">
          <Search aria-hidden />
          <input
            aria-label="Search scheduled tasks"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search tasks"
            value={query}
          />
        </label>
      </div>

      {error ? <div className="gui-chat-workspace-feedback is-error" role="alert">{error}</div> : null}

      <div className="gui-chat-workspace-list">
        {loading && jobs.length === 0 ? (
          <div className="gui-chat-workspace-empty" role="status">Loading scheduled tasks…</div>
        ) : visibleJobs.length === 0 ? (
          <div className="gui-chat-workspace-empty">
            <CalendarClock aria-hidden />
            <strong>{query.trim() ? "No matching tasks" : "No scheduled tasks yet"}</strong>
            <span>{query.trim() ? "Try a different search." : "Create a task to automate recurring work."}</span>
          </div>
        ) : visibleJobs.map((job) => {
          const state = jobState(job);
          const paused = state === "paused";
          return (
            <article className="gui-chat-workspace-row gui-chat-scheduled-task-row" key={job.id}>
              <div className="gui-chat-workspace-copy">
                <div className="gui-chat-workspace-title">
                  <span>{jobTitle(job)}</span>
                  <span className={`gui-chat-workspace-badge is-${state}`}>{state}</span>
                </div>
                <p>{job.prompt || job.script || "No prompt"}</p>
                <div className="gui-chat-scheduled-task-meta">
                  <span>{scheduleDisplay(job)}</span>
                  <span>Next: {formatTime(job.next_run_at)}</span>
                  <span>Last: {formatTime(job.last_run_at)}</span>
                </div>
                {job.last_error || job.last_delivery_error ? (
                  <div className="gui-chat-scheduled-task-error">
                    {job.last_error || job.last_delivery_error}
                  </div>
                ) : null}
              </div>
              <div className="gui-chat-workspace-actions">
                <button
                  aria-label={`${paused ? "Resume" : "Pause"} ${jobTitle(job)}`}
                  className="gui-chat-workspace-icon-button"
                  disabled={busyJob === job.id}
                  onClick={() => void pauseResume(job)}
                  type="button"
                >
                  {paused ? <Play aria-hidden /> : <Pause aria-hidden />}
                </button>
                <button
                  aria-label={`Edit ${jobTitle(job)}`}
                  className="gui-chat-workspace-icon-button"
                  disabled={busyJob === job.id}
                  onClick={() => {
                    setResources(EMPTY_RESOURCES);
                    setEditor({ job, form: editorFormFromJob(job) });
                  }}
                  type="button"
                >
                  <Pencil aria-hidden />
                </button>
                <button
                  aria-label={`Delete ${jobTitle(job)}`}
                  className="gui-chat-workspace-icon-button is-destructive"
                  disabled={busyJob === job.id}
                  onClick={() => setPendingDelete(job)}
                  type="button"
                >
                  <Trash2 aria-hidden />
                </button>
              </div>
            </article>
          );
        })}
      </div>

      {editor ? (
        <GuiChatWorkspaceDialog
          busy={busyJob === (editor.job?.id ?? "__create__")}
          description={editor.job ? "Update the task instructions, schedule, and delivery settings." : "Choose when this task runs and what Hermes should do."}
          onClose={() => setEditor(null)}
          title={editor.job ? "Edit scheduled task" : "New scheduled task"}
          wide
        >
          <div className="gui-chat-scheduled-task-editor">
            <CronJobFormFields
              autoFocus
              form={editor.form}
              idPrefix={editor.job ? "chat-cron-edit" : "chat-cron-create"}
              onChange={(form) => setEditor({ ...editor, form })}
              resources={resources}
            />
          </div>
          <div className="gui-chat-workspace-dialog-actions">
            <button disabled={busyJob !== null} onClick={() => setEditor(null)} type="button">Cancel</button>
            <button className="is-primary" disabled={busyJob !== null} onClick={() => void saveEditor()} type="button">
              {busyJob === (editor.job?.id ?? "__create__") ? "Saving…" : editor.job ? "Save changes" : "Create task"}
            </button>
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}

      {pendingDelete ? (
        <GuiChatWorkspaceDialog
          busy={busyJob === pendingDelete.id}
          description="This permanently removes the scheduled task. This action cannot be undone."
          onClose={() => setPendingDelete(null)}
          title={`Delete ${jobTitle(pendingDelete)}?`}
        >
          <div className="gui-chat-workspace-dialog-actions">
            <button disabled={busyJob !== null} onClick={() => setPendingDelete(null)} type="button">Cancel</button>
            <button className="is-destructive" disabled={busyJob !== null} onClick={() => void deleteJob()} type="button">
              {busyJob === pendingDelete.id ? "Deleting…" : "Delete"}
            </button>
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}
    </section>
  );
}

function sortJobs(jobs: CronJob[]): CronJob[] {
  return [...jobs].sort((left, right) => jobTitle(left).localeCompare(jobTitle(right)));
}

function jobTitle(job: CronJob): string {
  return String(job.name || job.prompt || job.script || job.id || "Scheduled task").trim();
}

function jobState(job: CronJob): string {
  return String(job.state || (job.enabled === false ? "paused" : "scheduled")).toLowerCase();
}

function scheduleDisplay(job: CronJob): string {
  return describeSchedule(
    job.schedule,
    String(job.schedule_display || job.schedule?.display || ""),
    ENGLISH_SCHEDULE_STRINGS,
  );
}

function formatTime(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}
