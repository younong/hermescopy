import {
  employeeDisplayName,
  type AutomationBlueprint,
  type AutomationBlueprintField,
  type CronDeliveryTarget,
  type CronJob,
  type CronJobMutation,
  type Employee,
  type ModelOptionsResponse,
  type SkillInfo,
  type ToolsetInfo,
} from "../../../../web/src/lib/api";
import type { CronJobEditorState } from "../../../../web/src/lib/cron-job-editor";
import {
  buildCronJobPayloadFromEditor,
  editorFormFromJob,
  emptyCronJobForm,
} from "../../../../web/src/lib/cron-job-editor";
import { cronJobHasExecutionContent } from "../../../../web/src/lib/cron-job";
import {
  describeSchedule,
  englishOrdinal,
  type ScheduleDescribeStrings,
} from "../../../../web/src/lib/schedule";
import type { HermesPluginSDK } from "../../../../web/src/plugins/sdk";
import type { ReactNode } from "react";

const SDK = window.__HERMES_PLUGIN_SDK__ as HermesPluginSDK;
if (!SDK) throw new Error("Hermes dashboard plugin SDK is unavailable");

const { fetchJSON, hooks, useConfirmDelete, useI18n, usePageHeader, useToast } = SDK;
const { useCallback, useEffect, useState } = hooks;
const {
  Badge,
  Button,
  Card,
  CardContent,
  ConfirmDialog,
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  H2,
  Input,
  Label,
  PluginSlot,
  Segmented,
  Select,
  SelectOption,
  Spinner,
  Toast,
} = SDK.components;

const API_ROOT = "/api/plugins/scheduled-tasks";
const JSON_HEADERS = { "Content-Type": "application/json" };
const LOCAL_DELIVERY: CronDeliveryTarget = {
  id: "local",
  name: "Local",
  home_target_set: true,
  home_env_var: null,
};

const taskApi = {
  getJobs: () => fetchJSON<CronJob[]>(`${API_ROOT}/jobs`),
  getDeliveryTargets: () =>
    fetchJSON<{ targets: CronDeliveryTarget[] }>(`${API_ROOT}/delivery-targets`),
  createJob: (job: CronJobMutation) =>
    fetchJSON<CronJob>(`${API_ROOT}/jobs`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(job),
    }),
  pauseJob: (id: string) =>
    fetchJSON<CronJob>(`${API_ROOT}/jobs/${encodeURIComponent(id)}/pause`, {
      method: "POST",
    }),
  updateJob: (id: string, updates: CronJobMutation) =>
    fetchJSON<CronJob>(`${API_ROOT}/jobs/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify({ updates }),
    }),
  resumeJob: (id: string) =>
    fetchJSON<CronJob>(`${API_ROOT}/jobs/${encodeURIComponent(id)}/resume`, {
      method: "POST",
    }),
  triggerJob: (id: string) =>
    fetchJSON<CronJob>(`${API_ROOT}/jobs/${encodeURIComponent(id)}/trigger`, {
      method: "POST",
    }),
  deleteJob: (id: string) =>
    fetchJSON<{ ok: boolean }>(`${API_ROOT}/jobs/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  getBlueprints: () =>
    fetchJSON<{ blueprints: AutomationBlueprint[] }>(`${API_ROOT}/blueprints`),
  instantiateBlueprint: (body: {
    blueprint: string;
    values: Record<string, string>;
  }) =>
    fetchJSON<CronJob>(`${API_ROOT}/blueprints/instantiate`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }),
};

function ScheduledTasksPage() {
  const { locale, t } = useI18n();
  const { setEnd } = usePageHeader();
  const { toast, showToast } = useToast();
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [view, setView] = useState<"jobs" | "blueprints">("jobs");
  const [loading, setLoading] = useState(true);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [createForm, setCreateForm] = useState<CronJobEditorState>(emptyCronJobForm);
  const [creating, setCreating] = useState(false);
  const [editJob, setEditJob] = useState<CronJob | null>(null);
  const [editForm, setEditForm] = useState<CronJobEditorState>(emptyCronJobForm);
  const [saving, setSaving] = useState(false);
  const [deliveryTargets, setDeliveryTargets] = useState<CronDeliveryTarget[]>([
    LOCAL_DELIVERY,
  ]);
  const [availableSkills, setAvailableSkills] = useState<SkillInfo[]>([]);
  const [availableToolsets, setAvailableToolsets] = useState<ToolsetInfo[]>([]);
  const [modelOptions, setModelOptions] = useState<ModelOptionsResponse | null>(null);
  const [employees, setEmployees] = useState<Employee[]>([]);

  const scheduleStrings: ScheduleDescribeStrings = {
    ...t.cron.scheduleDescribe,
    weekdaysShort: t.cron.scheduleModes.weekdaysShort,
    ordinal: locale === "en" ? englishOrdinal : (day: number) => String(day),
  };

  const loadJobs = useCallback(() => {
    taskApi
      .getJobs()
      .then(setJobs)
      .catch(() => showToast(t.common.loading, "error"))
      .finally(() => setLoading(false));
  }, [showToast, t.common.loading]);

  useEffect(() => {
    loadJobs();
  }, [loadJobs]);

  useEffect(() => {
    taskApi
      .getDeliveryTargets()
      .then(({ targets }) => setDeliveryTargets(targets.length ? targets : [LOCAL_DELIVERY]))
      .catch(() => setDeliveryTargets([LOCAL_DELIVERY]));
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetchJSON<SkillInfo[]>("/api/skills").catch(() => []),
      fetchJSON<ToolsetInfo[]>("/api/tools/toolsets").catch(() => []),
      fetchJSON<ModelOptionsResponse>("/api/model/options").catch(() => null),
      fetchJSON<{ employees: Employee[] }>("/api/employees?status=active&page=1&page_size=100").catch(() => ({ employees: [] })),
    ]).then(([skills, toolsets, options, employeeList]) => {
      if (cancelled) return;
      setAvailableSkills([...skills].sort((a, b) => a.name.localeCompare(b.name)));
      setAvailableToolsets([...toolsets].sort((a, b) => a.name.localeCompare(b.name)));
      setModelOptions(options);
      setEmployees(employeeList.employees ?? []);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setEnd(
      <Button className="uppercase" size="sm" onClick={() => setCreateModalOpen(true)}>
        {t.common.create}
      </Button>,
    );
    return () => setEnd(null);
  }, [setEnd, t.common.create]);

  const save = async (form: CronJobEditorState, job?: CronJob) => {
    const effectiveForm = {
      ...form,
      mode: form.employee_id || form.target_employee_ids.length ? "employee" as const : "custom" as const,
    };
    const payload = buildCronJobPayloadFromEditor(effectiveForm);
    if (!payload.schedule || (!payload.no_agent && !cronJobHasExecutionContent(payload))) {
      showToast(`${t.cron.prompt} & ${t.cron.schedule} required`, "error");
      return;
    }
    if (payload.no_agent && !payload.script) {
      showToast("no_agent jobs require a script", "error");
      return;
    }

    job ? setSaving(true) : setCreating(true);
    try {
      if (job) {
        await taskApi.updateJob(job.id, payload);
        setEditJob(null);
        showToast("Saved changes ✓", "success");
      } else {
        await taskApi.createJob(payload);
        setCreateForm(emptyCronJobForm());
        setCreateModalOpen(false);
        showToast(`${t.common.create} ✓`, "success");
      }
      loadJobs();
    } catch (error) {
      showToast(`${t.config.failedToSave}: ${String(error)}`, "error");
    } finally {
      job ? setSaving(false) : setCreating(false);
    }
  };

  const pauseResume = async (job: CronJob) => {
    try {
      const paused = jobState(job) === "paused";
      await (paused ? taskApi.resumeJob(job.id) : taskApi.pauseJob(job.id));
      showToast(
        `${paused ? t.cron.resume : t.cron.pause}: "${truncate(jobTitle(job), 30)}"`,
        "success",
      );
      loadJobs();
    } catch (error) {
      showToast(`${t.status.error}: ${String(error)}`, "error");
    }
  };

  const trigger = async (job: CronJob) => {
    try {
      await taskApi.triggerJob(job.id);
      showToast(`${t.cron.triggerNow}: "${truncate(jobTitle(job), 30)}"`, "success");
      loadJobs();
    } catch (error) {
      showToast(`${t.status.error}: ${String(error)}`, "error");
    }
  };

  const deleteJob = useCallback(
    async (id: string) => {
      const job = jobs.find((candidate) => candidate.id === id);
      try {
        await taskApi.deleteJob(id);
        showToast(`${t.common.delete}: "${job ? truncate(jobTitle(job), 30) : id}"`, "success");
        loadJobs();
      } catch (error) {
        showToast(`${t.status.error}: ${String(error)}`, "error");
        throw error;
      }
    },
    [jobs, loadJobs, showToast, t.common.delete, t.status.error],
  );
  const jobDelete = useConfirmDelete<string>({ onDelete: deleteJob });

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  const resources = {
    availableSkills,
    availableToolsets,
    modelOptions,
    deliveryTargets,
    employees,
  };
  const pendingJob = jobDelete.pendingId
    ? jobs.find((job) => job.id === jobDelete.pendingId)
    : null;

  return (
    <div className="flex flex-col gap-6">
      <PluginSlot name="cron:top" />
      <Toast toast={toast} />
      <Segmented
        value={view}
        onChange={setView}
        options={[
          { value: "jobs", label: "Jobs" },
          { value: "blueprints", label: "Blueprints" },
        ]}
      />

      {view === "blueprints" ? <AutomationBlueprints onCreated={loadJobs} /> : null}

      <ConfirmDialog
        open={jobDelete.isOpen}
        onCancel={jobDelete.cancel}
        onConfirm={() => void jobDelete.confirm()}
        title={t.cron.confirmDeleteTitle}
        description={
          pendingJob
            ? `"${truncate(jobTitle(pendingJob), 40)}" — ${t.cron.confirmDeleteMessage}`
            : t.cron.confirmDeleteMessage
        }
        loading={jobDelete.isDeleting}
        destructive
        confirmLabel={t.common.delete}
        cancelLabel={t.common.cancel}
      />

      <JobDialog
        form={createForm}
        loading={creating}
        onChange={setCreateForm}
        onClose={() => setCreateModalOpen(false)}
        onSave={() => void save(createForm)}
        open={createModalOpen}
        resources={resources}
        title={t.cron.newJob}
        submitLabel={creating ? t.common.creating : t.common.create}
      />

      {editJob ? (
        <JobDialog
          form={editForm}
          loading={saving}
          onChange={setEditForm}
          onClose={() => setEditJob(null)}
          onSave={() => void save(editForm, editJob)}
          open
          resources={resources}
          title="Edit job"
          submitLabel={saving ? t.common.loading : "Save changes"}
          footer={editJob.id}
        />
      ) : null}

      {view === "jobs" ? (
        <JobsList
          jobs={jobs}
          onDelete={jobDelete.requestDelete}
          onEdit={(job) => {
            setEditJob(job);
            setEditForm(editorFormFromJob(job));
          }}
          onPauseResume={(job) => void pauseResume(job)}
          onTrigger={(job) => void trigger(job)}
          scheduleStrings={scheduleStrings}
        />
      ) : null}

      <PluginSlot name="cron:bottom" />
    </div>
  );
}

interface JobDialogProps {
  footer?: string;
  form: CronJobEditorState;
  loading: boolean;
  onChange: (form: CronJobEditorState) => void;
  onClose: () => void;
  onSave: () => void;
  open: boolean;
  resources: {
    availableSkills: SkillInfo[];
    availableToolsets: ToolsetInfo[];
    modelOptions: ModelOptionsResponse | null;
    deliveryTargets: CronDeliveryTarget[];
    employees: Employee[];
  };
  submitLabel: string;
  title: string;
}

function JobDialog({
  footer,
  form,
  loading,
  onChange,
  onClose,
  onSave,
  open,
  resources,
  submitLabel,
  title,
}: JobDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(next: boolean) => !next && onClose()}>
      <DialogContent className="max-h-[90vh] max-w-3xl [transform:translate(-50%,-50%)] [translate:none] flex flex-col">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <div className="min-h-0 overflow-y-auto p-5 grid gap-4">
          <CronJobFormFields
            autoFocus
            form={form}
            idPrefix={footer ? "edit-cron" : "cron"}
            onChange={onChange}
            resources={resources}
          />
          <div className="flex items-center justify-between">
            <span className="truncate pr-4 text-xs text-muted-foreground font-mono-ui">
              {footer}
            </span>
            <Button
              className="uppercase"
              disabled={loading}
              onClick={onSave}
              prefix={loading ? <Spinner /> : undefined}
              size="sm"
            >
              {submitLabel}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function CronJobFormFields({
  autoFocus,
  form,
  idPrefix,
  onChange,
  resources,
}: {
  autoFocus?: boolean;
  form: CronJobEditorState;
  idPrefix: string;
  onChange: (form: CronJobEditorState) => void;
  resources: JobDialogProps["resources"];
}) {
  const { t } = useI18n();
  const update = <K extends keyof CronJobEditorState>(key: K, value: CronJobEditorState[K]) =>
    onChange({ ...form, [key]: value });
  const providers = (resources.modelOptions?.providers ?? []).filter(
    (provider) => provider.authenticated !== false,
  );
  const models = providers.find((provider) => provider.slug === form.provider)?.models ?? [];

  return (
    <>
      <Field label={t.cron.nameOptional} htmlFor={`${idPrefix}-name`}>
        <Input
          id={`${idPrefix}-name`}
          autoFocus={autoFocus}
          placeholder={t.cron.namePlaceholder}
          value={form.name}
          onChange={(event: { target: HTMLInputElement }) =>
            update("name", event.target.value)
          }
        />
      </Field>
      <Field label={t.cron.prompt} htmlFor={`${idPrefix}-prompt`}>
        <textarea
          id={`${idPrefix}-prompt`}
          className="flex min-h-[80px] w-full border border-border bg-background/40 px-3 py-2 text-sm font-courier shadow-sm placeholder:text-muted-foreground focus-visible:border-foreground/25 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30"
          placeholder={t.cron.promptPlaceholder}
          value={form.prompt}
          onChange={(event) => update("prompt", event.target.value)}
        />
      </Field>
      <ScheduleBuilder value={form.scheduleState} onChange={(value) => update("scheduleState", value)} />
      <Field label={t.cron.editor.employee} htmlFor={`${idPrefix}-employee`}>
        <Select
          id={`${idPrefix}-employee`}
          value={form.employee_id}
          onValueChange={(employee_id: string) =>
            onChange({ ...form, employee_id, target_employee_ids: [] })
          }
        >
          <SelectOption value="">{t.cron.editor.employeePlaceholder}</SelectOption>
          {resources.employees.map((employee) => (
            <SelectOption key={employee.employee_id} value={employee.employee_id}>
              {employeeDisplayName(employee, t.cron.editor.modeEmployee, employee.employee_id)}
            </SelectOption>
          ))}
        </Select>
      </Field>
      <Field label={`${t.cron.editor.employee} (multiple)`}>
        <NameCheckboxPicker
          available={resources.employees.map((employee) => ({
            name: employee.employee_id,
            description: employeeDisplayName(employee, t.cron.editor.modeEmployee, employee.employee_id),
          }))}
          emptyLabel={t.cron.editor.employeesEmpty}
          id={`${idPrefix}-target-employees`}
          onChange={(target_employee_ids) => onChange({
            ...form,
            target_employee_ids,
            employee_id: target_employee_ids.length ? "" : form.employee_id,
          })}
          selected={form.target_employee_ids}
        />
      </Field>
      <Field label={t.cron.deliverTo} htmlFor={`${idPrefix}-deliver`}>
        <Select
          id={`${idPrefix}-deliver`}
          value={form.deliver}
          onValueChange={(value: string) => update("deliver", value)}
        >
          {selectOptions(
            form.deliver,
            resources.deliveryTargets.map((target) => ({
              value: target.id,
              label:
                target.id === "local"
                  ? t.cron.delivery.local
                  : target.home_target_set
                    ? target.name
                    : `${target.name} — ${t.cron.delivery.needsHomeChannel ?? "set a home channel first"}`,
            })),
          )}
        </Select>
        {resources.deliveryTargets.every((target) => target.id === "local") ? (
          <p className="text-xs text-muted-foreground">
            {t.cron.delivery.noneConfigured ??
              "No messaging platforms configured. Set one up under Channels to deliver reports."}
          </p>
        ) : null}
      </Field>
      <Field label="Skills (optional)" htmlFor={`${idPrefix}-skills`}>
        <NameCheckboxPicker
          available={resources.availableSkills}
          emptyLabel="No skills installed for this profile."
          id={`${idPrefix}-skills`}
          onChange={(skills) => update("skills", skills)}
          selected={form.skills}
        />
        <p className="text-xs text-muted-foreground">
          Selected skills are loaded before the prompt runs — the cron sets when, the skill sets how.
        </p>
      </Field>
      <details className="border border-border bg-background/30 p-3" open>
        <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Advanced fields
        </summary>
        <div className="mt-3 grid gap-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Provider" htmlFor={`${idPrefix}-provider`}>
              <Select
                id={`${idPrefix}-provider`}
                value={form.provider}
                onValueChange={(provider: string) =>
                  onChange({ ...form, provider, model: "" })
                }
              >
                <SelectOption value="">Default</SelectOption>
                {selectOptions(
                  form.provider,
                  providers.map((provider) => ({ value: provider.slug, label: provider.name })),
                )}
              </Select>
            </Field>
            <Field label="Model" htmlFor={`${idPrefix}-model`}>
              <Select
                id={`${idPrefix}-model`}
                value={form.model}
                onValueChange={(value: string) => update("model", value)}
              >
                <SelectOption value="">Default</SelectOption>
                {selectOptions(
                  form.model,
                  models.map((model) => ({ value: model, label: model })),
                )}
              </Select>
            </Field>
          </div>
          <Field label="Base URL override" htmlFor={`${idPrefix}-base-url`}>
            <Input
              id={`${idPrefix}-base-url`}
              placeholder="https://api.example.com/v1"
              value={form.base_url}
              onChange={(event: { target: HTMLInputElement }) =>
                update("base_url", event.target.value)
              }
            />
          </Field>
          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-2">
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <input
                checked={form.no_agent}
                className="accent-foreground"
                onChange={(event) => update("no_agent", event.target.checked)}
                type="checkbox"
              />
              no_agent: run the script only and deliver stdout verbatim
            </label>
            <Field label="Script" htmlFor={`${idPrefix}-script`}>
              <Input
                id={`${idPrefix}-script`}
                placeholder="relative/path/in/scripts"
                value={form.script}
                onChange={(event: { target: HTMLInputElement }) =>
                  update("script", event.target.value)
                }
              />
            </Field>
          </div>
          <Field label="Workdir" htmlFor={`${idPrefix}-workdir`}>
            <Input
              id={`${idPrefix}-workdir`}
              placeholder="/absolute/project/path"
              value={form.workdir}
              onChange={(event: { target: HTMLInputElement }) =>
                update("workdir", event.target.value)
              }
            />
          </Field>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="context_from job IDs" htmlFor={`${idPrefix}-context-from`}>
              <textarea
                id={`${idPrefix}-context-from`}
                className="flex min-h-[64px] w-full border border-border bg-background/40 px-3 py-2 text-xs font-courier shadow-sm placeholder:text-muted-foreground focus-visible:border-foreground/25 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30"
                placeholder="one job id per line"
                value={form.context_from}
                onChange={(event) => update("context_from", event.target.value)}
              />
            </Field>
            <Field label="enabled_toolsets" htmlFor={`${idPrefix}-toolsets`}>
              <NameCheckboxPicker
                available={resources.availableToolsets}
                emptyLabel="No toolsets available."
                id={`${idPrefix}-toolsets`}
                onChange={(value) => update("enabled_toolsets", value)}
                selected={form.enabled_toolsets}
              />
            </Field>
          </div>
        </div>
      </details>
    </>
  );
}

function ScheduleBuilder({
  onChange,
  value,
}: {
  onChange: (state: CronJobEditorState["scheduleState"]) => void;
  value: CronJobEditorState["scheduleState"];
}) {
  const { t } = useI18n();
  const strings = t.cron.scheduleModes;
  const update = (patch: Partial<CronJobEditorState["scheduleState"]>) =>
    onChange({ ...value, ...patch });
  const modes = ["interval", "daily", "weekly", "monthly", "once", "custom"] as const;

  return (
    <div className="grid gap-3">
      <Field label={t.cron.scheduleMode ?? "Schedule"} htmlFor="cron-schedule-mode">
        <Select
          id="cron-schedule-mode"
          value={value.mode}
          onValueChange={(mode: CronJobEditorState["scheduleState"]["mode"]) =>
            update({ mode })
          }
        >
          {modes.map((mode) => (
            <SelectOption key={mode} value={mode}>
              {strings[mode]}
            </SelectOption>
          ))}
        </Select>
      </Field>
      {value.mode === "interval" ? (
        <div className="grid grid-cols-[1fr_1.4fr] gap-3">
          <Field label={strings.intervalEvery} htmlFor="cron-interval-value">
            <Input
              id="cron-interval-value"
              max={9999}
              min={1}
              type="number"
              value={String(value.intervalValue)}
              onChange={(event: { target: HTMLInputElement }) => {
                const parsed = Number.parseInt(event.target.value, 10);
                update({ intervalValue: Number.isFinite(parsed) && parsed > 0 ? parsed : 1 });
              }}
            />
          </Field>
          <Field label={strings.intervalUnit} htmlFor="cron-interval-unit">
            <Select
              id="cron-interval-unit"
              value={value.intervalUnit}
              onValueChange={(intervalUnit: CronJobEditorState["scheduleState"]["intervalUnit"]) =>
                update({ intervalUnit })
              }
            >
              <SelectOption value="minutes">{strings.unitMinutes}</SelectOption>
              <SelectOption value="hours">{strings.unitHours}</SelectOption>
              <SelectOption value="days">{strings.unitDays}</SelectOption>
            </Select>
          </Field>
        </div>
      ) : null}
      {value.mode === "daily" || value.mode === "monthly" ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {value.mode === "monthly" ? (
            <Field label={strings.dayOfMonth} htmlFor="cron-month-day">
              <Input
                id="cron-month-day"
                max={31}
                min={1}
                type="number"
                value={String(value.dayOfMonth)}
                onChange={(event: { target: HTMLInputElement }) => {
                  const parsed = Number.parseInt(event.target.value, 10);
                  update({ dayOfMonth: parsed >= 1 && parsed <= 31 ? parsed : 1 });
                }}
              />
            </Field>
          ) : null}
          <TimeField value={value.timeOfDay} onChange={(timeOfDay) => update({ timeOfDay })} />
        </div>
      ) : null}
      {value.mode === "weekly" ? (
        <>
          <div className="grid gap-2">
            <Label>{strings.weekdays}</Label>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={strings.weekdays}>
              {([0, 1, 2, 3, 4, 5, 6] as const).map((day) => {
                const selected = value.weekdays.includes(day);
                return (
                  <Button
                    aria-pressed={selected}
                    className="min-w-[2.5rem] text-xs uppercase font-mono-ui"
                    key={day}
                    onClick={() =>
                      update({
                        weekdays: selected
                          ? value.weekdays.filter((candidate) => candidate !== day)
                          : [...value.weekdays, day],
                      })
                    }
                    outlined={!selected}
                    size="sm"
                    type="button"
                  >
                    {strings.weekdaysShort[day]}
                  </Button>
                );
              })}
            </div>
          </div>
          <TimeField value={value.timeOfDay} onChange={(timeOfDay) => update({ timeOfDay })} />
        </>
      ) : null}
      {value.mode === "once" ? (
        <Field label={strings.onceAt} htmlFor="cron-once-at">
          <input
            id="cron-once-at"
            className="flex h-9 w-full border border-border bg-background/40 px-3 py-2 text-sm font-courier shadow-sm focus-visible:border-foreground/25 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30"
            type="datetime-local"
            value={value.onceAt}
            onChange={(event) => update({ onceAt: event.target.value })}
          />
        </Field>
      ) : null}
      {value.mode === "custom" ? (
        <Field label={strings.customLabel} htmlFor="cron-custom-expr">
          <Input
            className="font-mono-ui"
            id="cron-custom-expr"
            placeholder={strings.customPlaceholder}
            value={value.custom}
            onChange={(event: { target: HTMLInputElement }) =>
              update({ custom: event.target.value })
            }
          />
          <p className="text-xs text-muted-foreground">{strings.customHint}</p>
        </Field>
      ) : null}
      <p className="text-xs text-muted-foreground">
        <span className="opacity-70">{strings.preview}: </span>
        <span className="text-foreground font-mono-ui">
          {buildCronJobPayloadFromEditor({ ...emptyCronJobForm(), scheduleState: value }).schedule ||
            strings.previewEmpty}
        </span>
      </p>
    </div>
  );
}

function TimeField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { t } = useI18n();
  return (
    <Field label={t.cron.scheduleModes.timeOfDay} htmlFor="cron-time-of-day">
      <input
        id="cron-time-of-day"
        className="flex h-9 w-full border border-border bg-background/40 px-3 py-2 text-sm font-courier shadow-sm focus-visible:border-foreground/25 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30"
        type="time"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </Field>
  );
}

function NameCheckboxPicker({
  available,
  emptyLabel,
  id,
  onChange,
  selected,
}: {
  available: Array<{ name: string; description?: string | null }>;
  emptyLabel: string;
  id: string;
  onChange: (value: string[]) => void;
  selected: string[];
}) {
  const names = new Set(available.map((item) => item.name));
  const options = [
    ...selected.filter((name) => !names.has(name)).map((name) => ({ name, description: "" })),
    ...available,
  ];
  if (!options.length) return <p className="text-xs text-muted-foreground">{emptyLabel}</p>;
  return (
    <div id={id} className="max-h-36 overflow-y-auto border border-border bg-background/40 p-1">
      {options.map((item) => (
        <label
          className="flex cursor-pointer items-center gap-2 px-2 py-1 text-xs hover:bg-muted/40"
          key={item.name}
          title={item.description || undefined}
        >
          <input
            checked={selected.includes(item.name)}
            className="accent-foreground"
            onChange={(event) =>
              onChange(
                event.target.checked
                  ? [...selected, item.name]
                  : selected.filter((name) => name !== item.name),
              )
            }
            type="checkbox"
          />
          <span className="truncate font-mono-ui">{item.name}</span>
        </label>
      ))}
    </div>
  );
}

function JobsList({
  jobs,
  onDelete,
  onEdit,
  onPauseResume,
  onTrigger,
  scheduleStrings,
}: {
  jobs: CronJob[];
  onDelete: (id: string) => void;
  onEdit: (job: CronJob) => void;
  onPauseResume: (job: CronJob) => void;
  onTrigger: (job: CronJob) => void;
  scheduleStrings: ScheduleDescribeStrings;
}) {
  const { t } = useI18n();
  return (
    <div className="flex flex-col gap-3">
      <H2 className="flex items-center gap-2 text-muted-foreground" variant="sm">
        <span aria-hidden>◷</span>
        {t.cron.scheduledJobs} ({jobs.length})
      </H2>
      {!jobs.length ? (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            {t.cron.noJobs}
          </CardContent>
        </Card>
      ) : null}
      {jobs.map((job) => {
        const state = jobState(job);
        const title = jobTitle(job);
        const prompt = text(job.prompt);
        const mode = job.no_agent ? "no_agent" : job.script ? "script+agent" : "agent";
        const toolsets = Array.isArray(job.enabled_toolsets) ? job.enabled_toolsets.filter(Boolean) : [];
        return (
          <Card key={job.id}>
            <CardContent className="flex items-start gap-4 py-4">
              <div className="min-w-0 flex-1">
                <div className="mb-1 flex items-center gap-2">
                  <span className="truncate text-sm font-medium">{title}</span>
                  <Badge tone={stateTone(state)}>{state}</Badge>
                  {job.deliver && job.deliver !== "local" ? (
                    <Badge tone="outline">{job.deliver}</Badge>
                  ) : null}
                  {job.skills?.length ? (
                    <Badge title={job.skills.join(", ")} tone="outline">
                      {job.skills.length === 1 ? job.skills[0] : `${job.skills.length} skills`}
                    </Badge>
                  ) : null}
                  {mode !== "agent" ? <Badge tone="outline">{mode}</Badge> : null}
                  {job.model ? <Badge tone="outline">{job.model}</Badge> : null}
                  {toolsets.length ? <Badge tone="outline">{toolsets.length} toolsets</Badge> : null}
                </div>
                {text(job.name).trim() && prompt ? (
                  <p className="mb-1 truncate text-xs text-muted-foreground">{truncate(prompt, 100)}</p>
                ) : null}
                <div className="flex items-center gap-4 text-xs text-muted-foreground">
                  <span className="font-mono-ui">
                    {describeSchedule(
                      job.schedule,
                      text(job.schedule_display) || text(job.schedule?.display),
                      scheduleStrings,
                    )}
                  </span>
                  <span>repeat: {repeatDisplay(job)}</span>
                  <span>{t.cron.last}: {formatTime(job.last_run_at)}</span>
                  <span>{t.cron.next}: {formatTime(job.next_run_at)}</span>
                </div>
                {job.last_delivery_error ? (
                  <p className="mt-1 text-xs text-destructive">delivery: {job.last_delivery_error}</p>
                ) : null}
                {job.last_error ? <p className="mt-1 text-xs text-destructive">{job.last_error}</p> : null}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <ActionButton
                  label={state === "paused" ? t.cron.resume : t.cron.pause}
                  onClick={() => onPauseResume(job)}
                  tone={state === "paused" ? "text-success" : "text-warning"}
                >
                  {state === "paused" ? "▶" : "Ⅱ"}
                </ActionButton>
                <ActionButton label={t.cron.triggerNow} onClick={() => onTrigger(job)}>⚡</ActionButton>
                <ActionButton label="Edit job" onClick={() => onEdit(job)}>✎</ActionButton>
                <ActionButton label={t.common.delete} onClick={() => onDelete(job.id)} destructive>×</ActionButton>
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}

function ActionButton({
  children,
  destructive,
  label,
  onClick,
  tone,
}: {
  children: ReactNode;
  destructive?: boolean;
  label: string;
  onClick: () => void;
  tone?: string;
}) {
  return (
    <Button
      aria-label={label}
      className={tone}
      destructive={destructive}
      ghost
      onClick={onClick}
      size="icon"
      title={label}
    >
      {children}
    </Button>
  );
}

function AutomationBlueprints({ onCreated }: { onCreated: () => void }) {
  const { toast, showToast } = useToast();
  const [blueprints, setBlueprints] = useState<AutomationBlueprint[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    taskApi
      .getBlueprints()
      .then(({ blueprints: rows }) => !cancelled && setBlueprints(rows))
      .catch((error) => !cancelled && setLoadError(String(error)));
    return () => {
      cancelled = true;
    };
  }, []);
  if (loadError) return <p className="text-sm text-red-500">Couldn't load blueprints: {loadError}</p>;
  if (blueprints === null) return <div className="flex items-center gap-2 opacity-70"><Spinner /> Loading blueprints…</div>;
  if (!blueprints.length) return <p className="opacity-70">No automation blueprints available.</p>;
  return (
    <>
      <Toast toast={toast} />
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {blueprints.map((blueprint) => (
          <BlueprintCard
            blueprint={blueprint}
            key={blueprint.key}
            onCreated={onCreated}
            showToast={showToast}
          />
        ))}
      </div>
    </>
  );
}

function BlueprintCard({
  blueprint,
  onCreated,
  showToast,
}: {
  blueprint: AutomationBlueprint;
  onCreated: () => void;
  showToast: (message: string, type: "error" | "success") => void;
}) {
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>(() => initialValues(blueprint));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const job = await taskApi.instantiateBlueprint({ blueprint: blueprint.key, values });
      showToast(
        `${blueprint.title} scheduled${job.schedule_display ? ` — ${job.schedule_display}` : ""}`,
        "success",
      );
      setOpen(false);
      setValues(initialValues(blueprint));
      onCreated();
    } catch (cause) {
      setError(String(cause).replace(/^Error:\s*/, "").replace(/^\d+:\s*/, ""));
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <Card className="overflow-hidden">
      <CardContent className="space-y-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2"><span aria-hidden>✦</span><span className="font-medium">{blueprint.title}</span></div>
            <p className="mt-1 text-sm opacity-70">{blueprint.description}</p>
            <div className="mt-2 flex flex-wrap gap-1">
              {blueprint.tags.map((tag) => <Badge key={tag} tone="secondary">{tag}</Badge>)}
            </div>
          </div>
          <Button ghost={open} size="sm" onClick={() => setOpen((current) => !current)}>
            {open ? "Cancel" : "Set up"}
          </Button>
        </div>
        {open ? (
          <div className="space-y-3 border-t pt-3">
            {blueprint.fields.map((field) => (
              <Field key={field.name} label={field.label} htmlFor={`${blueprint.key}-${field.name}`}>
                <BlueprintField
                  field={field}
                  id={`${blueprint.key}-${field.name}`}
                  onChange={(value) => setValues((current) => ({ ...current, [field.name]: value }))}
                  value={values[field.name] ?? ""}
                />
                {field.help && field.type !== "text" ? <p className="text-xs opacity-60">{field.help}</p> : null}
              </Field>
            ))}
            {error ? <p className="text-sm text-red-500" role="alert">{error}</p> : null}
            <Button disabled={submitting} onClick={() => void submit()} prefix={submitting ? <Spinner /> : undefined}>
              Schedule it
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function BlueprintField({
  field,
  id,
  onChange,
  value,
}: {
  field: AutomationBlueprintField;
  id: string;
  onChange: (value: string) => void;
  value: string;
}) {
  if (field.type === "enum" || field.type === "weekdays") {
    return (
      <Select id={id} value={value} onValueChange={onChange}>
        {field.options.map((option) => <SelectOption key={option} value={option}>{option}</SelectOption>)}
      </Select>
    );
  }
  return (
    <Input
      id={id}
      type={field.type === "time" ? "time" : "text"}
      placeholder={field.help || field.label}
      value={value}
      onChange={(event: { target: HTMLInputElement }) => onChange(event.target.value)}
    />
  );
}

function Field({ children, htmlFor, label }: { children: ReactNode; htmlFor: string; label: string }) {
  return <div className="grid gap-2"><Label htmlFor={htmlFor}>{label}</Label>{children}</div>;
}

function selectOptions(current: string, options: Array<{ value: string; label: string }>) {
  const known = new Set(options.map((option) => option.value));
  const rows = [...options];
  if (current && !known.has(current)) rows.push({ value: current, label: current });
  return rows.map((option) => <SelectOption key={option.value} value={option.value}>{option.label}</SelectOption>);
}

function initialValues(blueprint: AutomationBlueprint) {
  return Object.fromEntries(blueprint.fields.map((field) => [field.name, field.default ?? ""]));
}

function text(value: unknown) {
  return typeof value === "string" ? value : "";
}
function truncate(value: string, limit: number) {
  return value.length > limit ? `${value.slice(0, limit)}...` : value;
}
function jobTitle(job: CronJob) {
  return text(job.name).trim() || truncate(text(job.prompt) || text(job.script), 60) || job.id || "Cron job";
}
function jobState(job: CronJob) {
  return text(job.state) || (job.enabled === false ? "disabled" : "scheduled");
}
function repeatDisplay(job: CronJob) {
  if (!job.repeat || job.repeat.times == null) return "forever";
  return job.repeat.completed ? `${job.repeat.completed}/${job.repeat.times}` : `${job.repeat.times} times`;
}
function formatTime(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}
function stateTone(state: string): "success" | "warning" | "destructive" | "secondary" {
  if (state === "enabled" || state === "scheduled") return "success";
  if (state === "paused") return "warning";
  if (state === "error" || state === "completed") return "destructive";
  return "secondary";
}

window.__HERMES_PLUGINS__?.register("scheduled-tasks", ScheduledTasksPage);
