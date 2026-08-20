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
import { guiChatTranslations, useI18n } from "@/i18n";
import { describeSchedule, englishOrdinal } from "@/lib/schedule";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";

const MAX_SCHEDULED_TASKS = 10;

const EMPTY_RESOURCES: CronJobFormResources = {
  availableSkills: [],
  availableToolsets: [],
  modelRegistrations: [],
  employees: [],
};
export function GuiChatScheduledTasksPane() {
  const { locale, t } = useI18n();
  const text = guiChatTranslations(t).scheduledTasks;
  const scheduleStrings = {
    ...t.cron.scheduleDescribe,
    weekdaysShort: t.cron.scheduleModes.weekdaysShort,
    ordinal: locale === "en" ? englishOrdinal : String,
  };
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
      api.getModelRegistrations().catch(() => ({ registrations: [] })),
      api.getEmployees({ status: "active" }).catch(() => ({ employees: [] })),
    ]).then(([skills, toolsets, registrations, employeeList]) => {
      if (cancelled) return;
      setResources({
        availableSkills: [...skills].sort((a, b) => a.name.localeCompare(b.name)),
        availableToolsets: [...toolsets].sort((a, b) => a.name.localeCompare(b.name)),
        modelRegistrations: registrations.registrations,
        employees: employeeList.employees,
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
      [jobTitle(job, text.fallbackTitle), job.prompt, job.script, job.state, job.last_status, job.last_error]
        .some((value) => String(value || "").toLowerCase().includes(normalized)),
    );
  }, [jobs, query, text.fallbackTitle]);

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
    if (
      editor.form.mode === "employee" &&
      !editor.form.employee_id &&
      editor.form.target_employee_ids.length === 0
    ) {
      setError(text.employeeRequired);
      return;
    }
    const payload = buildCronJobPayloadFromEditor(editor.form);
    if (!editor.job && jobs.length >= MAX_SCHEDULED_TASKS) {
      setError(text.limitReached);
      return;
    }
    if (!payload.schedule || (!payload.no_agent && !cronJobHasExecutionContent(payload))) {
      setError(text.validationRequired);
      return;
    }
    if (payload.no_agent && !payload.script) {
      setError(text.scriptRequired);
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
      if (isQuotaError(cause)) {
        void load().then(() => setError(text.limitReached));
      } else {
        setError(errorMessage(cause));
      }
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
    <section aria-label={text.title} className="gui-chat-workspace-pane" data-scheduled-tasks-pane>
      <header className="gui-chat-workspace-toolbar">
        <button
          className="gui-chat-workspace-primary-button"
          disabled={jobs.length >= MAX_SCHEDULED_TASKS}
          onClick={() => {
            setResources(EMPTY_RESOURCES);
            setEditor({ job: null, form: emptyCronJobForm() });
          }}
          type="button"
        >
          <Plus aria-hidden />{text.newTask}
        </button>
        <button
          aria-label={text.refresh}
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
          <h1>{text.title}</h1>
          <p>{text.description}</p>
          <p className="gui-chat-workspace-muted">{text.limitHint}</p>
          <p className="gui-chat-workspace-muted">{text.countLabel.replace("{count}", String(jobs.length))}</p>
          {jobs.length >= MAX_SCHEDULED_TASKS ? (
            <p className="gui-chat-workspace-feedback is-error">{text.limitReached}</p>
          ) : null}
        </div>
        <label className="gui-chat-workspace-search">
          <Search aria-hidden />
          <input
            aria-label={text.search}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={text.searchPlaceholder}
            value={query}
          />
        </label>
      </div>

      {error ? <div className="gui-chat-workspace-feedback is-error" role="alert">{error}</div> : null}

      <div className="gui-chat-workspace-list">
        {loading && jobs.length === 0 ? (
          <div className="gui-chat-workspace-empty" role="status">{text.loading}</div>
        ) : visibleJobs.length === 0 ? (
          <div className="gui-chat-workspace-empty">
            <CalendarClock aria-hidden />
            <strong>{query.trim() ? text.noMatching : text.none}</strong>
            <span>{query.trim() ? text.differentSearch : text.createHint}</span>
          </div>
        ) : visibleJobs.map((job) => {
          const state = jobState(job);
          const paused = state === "paused";
          return (
            <article className="gui-chat-workspace-row gui-chat-scheduled-task-row" key={job.id}>
              <div className="gui-chat-workspace-copy">
                <div className="gui-chat-workspace-title">
                  <span>{jobTitle(job, text.fallbackTitle)}</span>
                  <span className={`gui-chat-workspace-badge is-${state}`}>{state}</span>
                </div>
                <p>{job.prompt || job.script || text.noPrompt}</p>
                <div className="gui-chat-scheduled-task-meta">
                  <span>{scheduleDisplay(job, scheduleStrings)}</span>
                  <span>{text.next}: {formatTime(job.next_run_at, locale)}</span>
                  <span>{text.last}: {formatTime(job.last_run_at, locale)}</span>
                </div>
                {job.last_error || job.last_delivery_error ? (
                  <div className="gui-chat-scheduled-task-error">
                    {job.last_error || job.last_delivery_error}
                  </div>
                ) : null}
              </div>
              <div className="gui-chat-workspace-actions">
                <button
                  aria-label={(paused ? text.resumeNamed : text.pauseNamed).replace("{name}", jobTitle(job, text.fallbackTitle))}
                  className="gui-chat-workspace-icon-button"
                  disabled={busyJob === job.id}
                  onClick={() => void pauseResume(job)}
                  type="button"
                >
                  {paused ? <Play aria-hidden /> : <Pause aria-hidden />}
                </button>
                <button
                  aria-label={text.editNamed.replace("{name}", jobTitle(job, text.fallbackTitle))}
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
                  aria-label={text.deleteNamed.replace("{name}", jobTitle(job, text.fallbackTitle))}
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
          description={editor.job ? text.editDescription : text.createDescription}
          onClose={() => setEditor(null)}
          title={editor.job ? text.editTask : text.newTask}
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
            <button disabled={busyJob !== null} onClick={() => setEditor(null)} type="button">{t.common.cancel}</button>
            <button className="is-primary" disabled={busyJob !== null} onClick={() => void saveEditor()} type="button">
              {busyJob === (editor.job?.id ?? "__create__") ? text.saving : editor.job ? text.saveChanges : text.createTask}
            </button>
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}

      {pendingDelete ? (
        <GuiChatWorkspaceDialog
          busy={busyJob === pendingDelete.id}
          description={text.deleteDescription}
          onClose={() => setPendingDelete(null)}
          title={text.deleteTitle.replace("{name}", jobTitle(pendingDelete, text.fallbackTitle))}
        >
          <div className="gui-chat-workspace-dialog-actions">
            <button disabled={busyJob !== null} onClick={() => setPendingDelete(null)} type="button">{t.common.cancel}</button>
            <button className="is-destructive" disabled={busyJob !== null} onClick={() => void deleteJob()} type="button">
              {busyJob === pendingDelete.id ? text.deleting : t.common.delete}
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

function jobTitle(job: CronJob, fallback = ""): string {
  return String(job.name || job.prompt || job.script || job.id || fallback).trim();
}

function jobState(job: CronJob): string {
  return String(job.state || (job.enabled === false ? "paused" : "scheduled")).toLowerCase();
}

function scheduleDisplay(
  job: CronJob,
  strings: Parameters<typeof describeSchedule>[2],
): string {
  return describeSchedule(
    job.schedule,
    String(job.schedule_display || job.schedule?.display || ""),
    strings,
  );
}

function formatTime(value: string | null | undefined, locale: string): string {
  return value ? new Date(value).toLocaleString(locale === "zh" ? "zh-CN" : "en") : "—";
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

function isQuotaError(cause: unknown): boolean {
  return typeof cause === "object" && cause !== null && "status" in cause
    ? Number((cause as { status?: unknown }).status) === 409
    : errorMessage(cause).includes("Scheduled task limit reached");
}
