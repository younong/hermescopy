import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import { Clock, Pause, Pencil, Play, Trash2, Zap } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { api } from "@/lib/api";
import type {
  CronJob,
  CronDeliveryTarget,
  ModelOptionsResponse,
  SkillInfo,
  ToolsetInfo,
} from "@/lib/api";
import { cronJobHasExecutionContent } from "@/lib/cron-job";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import {
  CronJobFormFields,
} from "@/components/CronJobEditor";
import {
  buildCronJobPayloadFromEditor,
  editorFormFromJob,
  emptyCronJobForm,
  type CronJobEditorState,
} from "@/lib/cron-job-editor";
import {
  describeSchedule,
  englishOrdinal,
  type ScheduleDescribeStrings,
} from "@/lib/schedule";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";
import {
  Dialog,
  DialogHeader,
  DialogTitle,
} from "@nous-research/ui/ui/components/dialog";
import { CenteredDialogContent } from "@/components/CenteredDialogContent";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { PluginSlot } from "@/plugins";
import { Segmented } from "@nous-research/ui/ui/components/segmented";
import { AutomationBlueprints } from "@/components/AutomationBlueprints";

function formatTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString();
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function truncateText(value: string, maxLength: number): string {
  return value.length > maxLength
    ? value.slice(0, maxLength) + "..."
    : value;
}

function getJobPrompt(job: CronJob): string {
  return asText(job.prompt);
}

function getJobName(job: CronJob): string {
  return asText(job.name).trim();
}

function getJobTitle(job: CronJob): string {
  const name = getJobName(job);
  if (name) return name;

  const prompt = getJobPrompt(job);
  if (prompt) return truncateText(prompt, 60);

  const script = asText(job.script);
  if (script) return truncateText(script, 60);

  return job.id || "Cron job";
}

function getJobScheduleDisplay(
  job: CronJob,
  strings: ScheduleDescribeStrings,
): string {
  // Prefer a structured render so cron expressions like
  // ``30 14 * * 1,3,5`` surface as "Weekly on Mon, Wed, Fri at 14:30"
  // in the list instead of the raw five-field gibberish. Falls back
  // through the existing chain (``schedule_display`` from the backend,
  // then the structured ``display`` field, then the raw ``expr``) so
  // legacy job rows still render *something* meaningful.
  return describeSchedule(
    job.schedule,
    asText(job.schedule_display) || asText(job.schedule?.display),
    strings,
  );
}

function getJobState(job: CronJob): string {
  return asText(job.state) || (job.enabled === false ? "disabled" : "scheduled");
}

function getRepeatDisplay(job: CronJob): string {
  const repeat = job.repeat;
  if (!repeat || repeat.times == null) return "forever";
  const completed = repeat.completed ?? 0;
  return completed > 0 ? `${completed}/${repeat.times}` : `${repeat.times} times`;
}

function getJobMode(job: CronJob): string {
  if (job.no_agent) return "no_agent";
  if (job.script) return "script+agent";
  return "agent";
}

function getModelDisplay(job: CronJob): string {
  const provider = asText(job.provider);
  const model = asText(job.model);
  if (provider && model) return `${provider}/${model}`;
  return model || provider;
}

const STATUS_TONE: Record<string, "success" | "warning" | "destructive"> = {
  enabled: "success",
  scheduled: "success",
  paused: "warning",
  error: "destructive",
  completed: "destructive",
};

export default function CronPage() {
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [view, setView] = useState<"jobs" | "blueprints">("jobs");
  const [loading, setLoading] = useState(true);
  const { toast, showToast } = useToast();
  const { t, locale } = useI18n();
  const { setEnd } = usePageHeader();

  // Translation surface for the human-readable schedule describer.
  // English ordinals are a special case ("1st", "2nd", "23rd"); every
  // other locale falls back to the plain numeric form, which avoids
  // shipping incorrect grammar (e.g. naive "1th"/"2th" suffixes that
  // don't exist in most languages).
  //
  // Built inline (not memoized) — the cron page renders a small job
  // list, this is single-digit microseconds, and a useMemo here would
  // just add boilerplate.
  const scheduleDescribeStrings: ScheduleDescribeStrings = {
    ...t.cron.scheduleDescribe,
    weekdaysShort: t.cron.scheduleModes.weekdaysShort,
    ordinal: locale === "en" ? englishOrdinal : (n: number) => String(n),
  };

  // New job modal state
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [createForm, setCreateForm] = useState<CronJobEditorState>(
    emptyCronJobForm,
  );
  const [deliveryTargets, setDeliveryTargets] = useState<CronDeliveryTarget[]>([
    { id: "local", name: "Local", home_target_set: true, home_env_var: null },
  ]);
  const [creating, setCreating] = useState(false);

  // Edit job modal state
  const [editJob, setEditJob] = useState<CronJob | null>(null);
  const [editForm, setEditForm] = useState<CronJobEditorState>(
    emptyCronJobForm,
  );
  const [saving, setSaving] = useState(false);

  // Skills installed for the authenticated Owner, used by the attach-skill
  // selector. A job's current skills remain visible even if not installed.
  const [availableSkills, setAvailableSkills] = useState<SkillInfo[]>([]);
  const [availableToolsets, setAvailableToolsets] = useState<ToolsetInfo[]>([]);
  const [modelOptions, setModelOptions] = useState<ModelOptionsResponse | null>(null);

  const openEditModal = useCallback((job: CronJob) => {
    setEditJob(job);
    setEditForm(editorFormFromJob(job));
  }, []);

  const loadJobs = useCallback(() => {
    api
      .getCronJobs()
      .then(setJobs)
      .catch(() => showToast(t.common.loading, "error"))
      .finally(() => setLoading(false));
  }, [showToast, t.common.loading]);

  useEffect(() => {
    api
      .getCronDeliveryTargets()
      .then((res) => setDeliveryTargets(res.targets))
      .catch(() =>
        // Fall back to local-only so the modal still works if the endpoint fails.
        setDeliveryTargets([
          { id: "local", name: "Local", home_target_set: true, home_env_var: null },
        ]),
      );
  }, []);

  useEffect(() => {
    loadJobs();
  }, [loadJobs]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.getSkills().catch(() => []),
      api.getToolsets().catch(() => []),
      api.getModelOptions().catch(() => null),
    ]).then(([skills, toolsets, options]) => {
      if (cancelled) return;
      setAvailableSkills([...skills].sort((a, b) => a.name.localeCompare(b.name)));
      setAvailableToolsets([...toolsets].sort((a, b) => a.name.localeCompare(b.name)));
      setModelOptions(options);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleCreate = async () => {
    const payload = buildCronJobPayloadFromEditor(createForm);
    if (
      !payload.schedule ||
      (!payload.no_agent && !cronJobHasExecutionContent(payload))
    ) {
      showToast(`${t.cron.prompt} & ${t.cron.schedule} required`, "error");
      return;
    }
    if (payload.no_agent && !payload.script) {
      showToast("no_agent jobs require a script", "error");
      return;
    }
    setCreating(true);
    try {
      await api.createCronJob(payload);
      showToast(t.common.create + " ✓", "success");
      setCreateForm(emptyCronJobForm());
      setCreateModalOpen(false);
      loadJobs();
    } catch (e) {
      showToast(`${t.config.failedToSave}: ${e}`, "error");
    } finally {
      setCreating(false);
    }
  };

  const handleEdit = async () => {
    if (!editJob) return;
    const payload = buildCronJobPayloadFromEditor(editForm);
    if (
      !payload.schedule ||
      (!payload.no_agent && !cronJobHasExecutionContent(payload))
    ) {
      showToast(`${t.cron.prompt} & ${t.cron.schedule} required`, "error");
      return;
    }
    if (payload.no_agent && !payload.script) {
      showToast("no_agent jobs require a script", "error");
      return;
    }
    setSaving(true);
    try {
      await api.updateCronJob(editJob.id, payload);
      showToast("Saved changes ✓", "success");
      setEditJob(null);
      loadJobs();
    } catch (e) {
      showToast(`${t.config.failedToSave}: ${e}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const handlePauseResume = async (job: CronJob) => {
    try {
      const isPaused = getJobState(job) === "paused";
      if (isPaused) {
        await api.resumeCronJob(job.id);
        showToast(
          `${t.cron.resume}: "${truncateText(getJobTitle(job), 30)}"`,
          "success",
        );
      } else {
        await api.pauseCronJob(job.id);
        showToast(
          `${t.cron.pause}: "${truncateText(getJobTitle(job), 30)}"`,
          "success",
        );
      }
      loadJobs();
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    }
  };

  const handleTrigger = async (job: CronJob) => {
    try {
      await api.triggerCronJob(job.id);
      showToast(
        `${t.cron.triggerNow}: "${truncateText(getJobTitle(job), 30)}"`,
        "success",
      );
      loadJobs();
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    }
  };

  const jobDelete = useConfirmDelete({
    onDelete: useCallback(
      async (id: string) => {
        const job = jobs.find((j) => j.id === id);
        try {
          await api.deleteCronJob(id);
          showToast(
            `${t.common.delete}: "${job ? truncateText(getJobTitle(job), 30) : id}"`,
            "success",
          );
          loadJobs();
        } catch (e) {
          showToast(`${t.status.error}: ${e}`, "error");
          throw e;
        }
      },
      [jobs, loadJobs, showToast, t.common.delete, t.status.error],
    ),
  });

  // Put "Create" button in page header
  useLayoutEffect(() => {
    setEnd(
      <Button
        className="uppercase"
        size="sm"
        onClick={() => setCreateModalOpen(true)}
      >
        {t.common.create}
      </Button>,
    );
    return () => {
      setEnd(null);
    };
  }, [setEnd, t.common.create]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  const pendingJob = jobDelete.pendingId
    ? jobs.find((j) => j.id === jobDelete.pendingId)
    : null;

  return (
    <div className="flex flex-col gap-6">
      <PluginSlot name="cron:top" />
      <Toast toast={toast} />

      <Segmented
        value={view}
        onChange={(v) => setView(v as "jobs" | "blueprints")}
        options={[
          { value: "jobs", label: "Jobs" },
          { value: "blueprints", label: "Blueprints" },
        ]}
      />

      {view === "blueprints" && (
        <AutomationBlueprints onCreated={loadJobs} />
      )}


      <DeleteConfirmDialog
        open={jobDelete.isOpen}
        onCancel={jobDelete.cancel}
        onConfirm={jobDelete.confirm}
        title={t.cron.confirmDeleteTitle}
        description={
          pendingJob
            ? `"${truncateText(getJobTitle(pendingJob), 40)}" — ${
                t.cron.confirmDeleteMessage
              }`
            : t.cron.confirmDeleteMessage
        }
        loading={jobDelete.isDeleting}
      />

      <Dialog
        open={createModalOpen}
        onOpenChange={(open) => !open && setCreateModalOpen(false)}
      >
        <CenteredDialogContent className="max-w-3xl max-h-[90vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>{t.cron.newJob}</DialogTitle>
          </DialogHeader>

          <div className="min-h-0 overflow-y-auto p-5 grid gap-4">
            <CronJobFormFields
              idPrefix="cron"
              autoFocus
              form={createForm}
              onChange={setCreateForm}
              resources={{
                availableSkills,
                availableToolsets,
                modelOptions,
                deliveryTargets,
              }}
            />

            <div className="flex justify-end">
              <Button
                className="uppercase"
                size="sm"
                onClick={handleCreate}
                disabled={creating}
                prefix={creating ? <Spinner /> : undefined}
              >
                {creating ? t.common.creating : t.common.create}
              </Button>
            </div>
          </div>
        </CenteredDialogContent>
      </Dialog>

      {editJob && (
        <Dialog open onOpenChange={(open) => !open && setEditJob(null)}>
          <CenteredDialogContent className="max-w-3xl max-h-[90vh] flex flex-col">
            <DialogHeader>
              <DialogTitle>Edit job</DialogTitle>
            </DialogHeader>

            <div className="min-h-0 overflow-y-auto p-5 grid gap-4">
              <CronJobFormFields
                idPrefix="edit-cron"
                autoFocus
                form={editForm}
                onChange={setEditForm}
                resources={{
                  availableSkills,
                  availableToolsets,
                  modelOptions,
                  deliveryTargets,
                }}
              />

              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground font-mono-ui truncate pr-4">
                  {editJob.id}
                </span>
                <Button
                  className="uppercase"
                  size="sm"
                  onClick={handleEdit}
                  disabled={saving}
                  prefix={saving ? <Spinner /> : undefined}
                >
                  {saving ? t.common.loading : "Save changes"}
                </Button>
              </div>
            </div>
          </CenteredDialogContent>
        </Dialog>
      )}

      {view === "jobs" && (
      <div className="flex flex-col gap-3">
        <H2
          variant="sm"
          className="flex items-center gap-2 text-muted-foreground"
        >
          <Clock className="h-4 w-4" />
          {t.cron.scheduledJobs} ({jobs.length})
        </H2>

        {jobs.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              {t.cron.noJobs}
            </CardContent>
          </Card>
        )}

        {jobs.map((job) => {
          const state = getJobState(job);
          const promptText = getJobPrompt(job);
          const title = getJobTitle(job);
          const hasName = Boolean(getJobName(job));
          const deliver = asText(job.deliver);
          const mode = getJobMode(job);
          const modelDisplay = getModelDisplay(job);
          const toolsets = Array.isArray(job.enabled_toolsets)
            ? job.enabled_toolsets.filter(Boolean)
            : [];

          return (
            <Card key={job.id}>
              <CardContent className="flex items-start gap-4 py-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="font-medium text-sm truncate">
                      {title}
                    </span>
                    <Badge tone={STATUS_TONE[state] ?? "secondary"}>
                      {state}
                    </Badge>
                    {deliver && deliver !== "local" && (
                      <Badge tone="outline">{deliver}</Badge>
                    )}
                    {Array.isArray(job.skills) && job.skills.length > 0 && (
                      <Badge tone="outline" title={job.skills.join(", ")}>
                        {job.skills.length === 1
                          ? job.skills[0]
                          : `${job.skills.length} skills`}
                      </Badge>
                    )}
                    {mode !== "agent" && (
                      <Badge tone="outline">{mode}</Badge>
                    )}
                    {modelDisplay && (
                      <Badge tone="outline" title={modelDisplay}>
                        model
                      </Badge>
                    )}
                    {toolsets.length > 0 && (
                      <Badge tone="outline" title={toolsets.join(", ")}>
                        {toolsets.length} toolsets
                      </Badge>
                    )}
                  </div>
                  {hasName && promptText && (
                    <p className="text-xs text-muted-foreground truncate mb-1">
                      {truncateText(promptText, 100)}
                    </p>
                  )}
                  <div className="flex items-center gap-4 text-xs text-muted-foreground">
                    <span className="font-mono-ui">
                      {getJobScheduleDisplay(job, scheduleDescribeStrings)}
                    </span>
                    <span>repeat: {getRepeatDisplay(job)}</span>
                    <span>
                      {t.cron.last}: {formatTime(job.last_run_at)}
                    </span>
                    <span>
                      {t.cron.next}: {formatTime(job.next_run_at)}
                    </span>
                  </div>
                  {job.last_delivery_error && (
                    <p className="text-xs text-destructive mt-1">
                      delivery: {job.last_delivery_error}
                    </p>
                  )}
                  {job.last_error && (
                    <p className="text-xs text-destructive mt-1">
                      {job.last_error}
                    </p>
                  )}
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  <Button
                    ghost
                    size="icon"
                    title={state === "paused" ? t.cron.resume : t.cron.pause}
                    aria-label={
                      state === "paused" ? t.cron.resume : t.cron.pause
                    }
                    onClick={() => handlePauseResume(job)}
                    className={
                      state === "paused" ? "text-success" : "text-warning"
                    }
                  >
                    {state === "paused" ? <Play /> : <Pause />}
                  </Button>

                  <Button
                    ghost
                    size="icon"
                    title={t.cron.triggerNow}
                    aria-label={t.cron.triggerNow}
                    onClick={() => handleTrigger(job)}
                  >
                    <Zap />
                  </Button>

                  <Button
                    ghost
                    size="icon"
                    title="Edit job"
                    aria-label="Edit job"
                    onClick={() => openEditModal(job)}
                  >
                    <Pencil />
                  </Button>

                  <Button
                    ghost
                    destructive
                    size="icon"
                    title={t.common.delete}
                    aria-label={t.common.delete}
                    onClick={() => jobDelete.requestDelete(job.id)}
                  >
                    <Trash2 />
                  </Button>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>
      )}

      <PluginSlot name="cron:bottom" />
    </div>
  );
}
