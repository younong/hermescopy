import { NameCheckboxPicker } from "@/components/NameCheckboxPicker";
import { ScheduleBuilder } from "@/components/ScheduleBuilder";
import { guiChatTranslations, useI18n } from "@/i18n";
import {
  employeeDisplayName,
  type Employee,
  type ModelRegistration,
  type SkillInfo,
  type ToolsetInfo,
} from "@/lib/api";
import type { CronJobEditorMode, CronJobEditorState } from "@/lib/cron-job-editor";

export interface CronJobFormResources {
  availableSkills: SkillInfo[];
  availableToolsets: ToolsetInfo[];
  modelRegistrations: ModelRegistration[];
  employees: Employee[];
}

const FIELD_INPUT =
  "h-9 w-full rounded-lg border border-black/[0.08] bg-white px-3 text-[13px] text-[#202124] outline-none transition placeholder:text-[#a0a3a8] focus:border-black/20";
const FIELD_TEXTAREA =
  "min-h-[80px] w-full rounded-lg border border-black/[0.08] bg-white p-3 text-[13px] text-[#202124] outline-none transition placeholder:text-[#a0a3a8] focus:border-black/20";

function Field({
  children,
  htmlFor,
  label,
}: {
  children: React.ReactNode;
  htmlFor?: string;
  label: string;
}) {
  return (
    <div className="grid gap-1.5">
      <label className="text-[12px] text-[#85888e]" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
    </div>
  );
}

function CronEmployeeFields({
  idPrefix,
  form,
  employees,
  onChange,
}: {
  idPrefix: string;
  form: CronJobEditorState;
  employees: Employee[];
  onChange: (form: CronJobEditorState) => void;
}) {
  const { t } = useI18n();
  const employeeText = guiChatTranslations(t).employees;
  const active = employees.filter((employee) => employee.lifecycle_status === "active");
  const known = new Set(active.map((employee) => employee.employee_id));
  return (
    <>
      <Field htmlFor={`${idPrefix}-employee`} label={t.cron.editor.employee}>
        <select
          className={FIELD_INPUT}
          id={`${idPrefix}-employee`}
          onChange={(event) =>
            onChange({ ...form, employee_id: event.target.value, target_employee_ids: [] })
          }
          value={form.employee_id}
        >
          <option value="">{t.cron.editor.employeePlaceholder}</option>
          {active.map((employee) => (
            <option key={employee.employee_id} value={employee.employee_id}>
              {employeeDisplayName(employee, employeeText.aiAssistant, employeeText.unnamed)}
            </option>
          ))}
          {form.employee_id && !known.has(form.employee_id) ? (
            <option value={form.employee_id}>{form.employee_id}</option>
          ) : null}
        </select>
        {active.length === 0 ? (
          <p className="text-[12px] text-[#a0a3a8]">{t.cron.editor.employeesEmpty}</p>
        ) : null}
      </Field>
      <Field label={`${t.cron.editor.employee} (multiple)`}>
        <NameCheckboxPicker
          id={`${idPrefix}-target-employees`}
          available={active.map((employee) => ({
            name: employee.employee_id,
            description: employeeDisplayName(employee, employeeText.aiAssistant, employeeText.unnamed),
          }))}
          selected={form.target_employee_ids}
          onChange={(target_employee_ids) =>
            onChange({
              ...form,
              target_employee_ids,
              employee_id: target_employee_ids.length ? "" : form.employee_id,
            })
          }
          emptyLabel={t.cron.editor.employeesEmpty}
        />
        <p className="text-[12px] text-[#a0a3a8]">Select multiple employees for one scheduled run.</p>
      </Field>
    </>
  );
}

function CronCustomFields({
  idPrefix,
  form,
  resources,
  onChange,
}: {
  idPrefix: string;
  form: CronJobEditorState;
  resources: CronJobFormResources;
  onChange: (form: CronJobEditorState) => void;
}) {
  const { t } = useI18n();
  const update = <K extends keyof CronJobEditorState>(
    key: K,
    next: CronJobEditorState[K],
  ) => onChange({ ...form, [key]: next });
  const registrations = resources.modelRegistrations.filter(
    (registration) => registration.kind === "chat",
  );
  const selectedRegistration = registrations.find(
    (registration) =>
      registration.provider === form.provider && registration.model === form.model,
  );
  const selectValue = selectedRegistration?.id ?? (form.model ? "__current__" : "");
  const currentLabel = [form.provider, form.model].filter(Boolean).join(" / ");

  return (
    <>
      <Field htmlFor={`${idPrefix}-model`} label={t.cron.editor.model}>
        <select
          className={FIELD_INPUT}
          id={`${idPrefix}-model`}
          onChange={(event) => {
            const registration = registrations.find((item) => item.id === event.target.value);
            onChange({
              ...form,
              provider: registration?.provider ?? "",
              model: registration?.model ?? "",
            });
          }}
          value={selectValue}
        >
          <option value="">{t.cron.editor.modelDefault}</option>
          {registrations.map((registration) => (
            <option key={registration.id} value={registration.id}>
              {registration.name || `${registration.provider} / ${registration.model}`}
            </option>
          ))}
          {selectValue === "__current__" ? (
            <option value="__current__">{currentLabel}</option>
          ) : null}
        </select>
      </Field>

      <Field label={t.cron.editor.skills}>
        <NameCheckboxPicker
          id={`${idPrefix}-skills`}
          available={resources.availableSkills}
          selected={form.skills}
          onChange={(skills) => update("skills", skills)}
          emptyLabel={t.cron.editor.skillsEmpty}
        />
        <p className="text-[12px] text-[#a0a3a8]">{t.cron.editor.skillsHint}</p>
      </Field>

      <details className="rounded-lg border border-black/[0.08] bg-[#f7f8fa] p-3">
        <summary className="cursor-pointer text-[12px] font-medium text-[#85888e]">
          {t.cron.editor.advanced}
        </summary>
        <div className="mt-3 grid gap-3">
          <Field htmlFor={`${idPrefix}-base-url`} label={t.cron.editor.baseUrl}>
            <input
              className={FIELD_INPUT}
              id={`${idPrefix}-base-url`}
              placeholder={t.cron.editor.baseUrlPlaceholder}
              value={form.base_url}
              onChange={(event) => update("base_url", event.target.value)}
            />
          </Field>

          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-2">
            <label className="flex items-center gap-2 text-[12px] text-[#85888e]">
              <input
                type="checkbox"
                className="accent-[#202124]"
                checked={form.no_agent}
                onChange={(event) => update("no_agent", event.target.checked)}
              />
              {t.cron.editor.noAgent}
            </label>
            <Field htmlFor={`${idPrefix}-script`} label={t.cron.editor.script}>
              <input
                className={FIELD_INPUT}
                id={`${idPrefix}-script`}
                placeholder={t.cron.editor.scriptPlaceholder}
                value={form.script}
                onChange={(event) => update("script", event.target.value)}
              />
            </Field>
          </div>

          <Field htmlFor={`${idPrefix}-workdir`} label={t.cron.editor.workdir}>
            <input
              className={FIELD_INPUT}
              id={`${idPrefix}-workdir`}
              placeholder={t.cron.editor.workdirPlaceholder}
              value={form.workdir}
              onChange={(event) => update("workdir", event.target.value)}
            />
          </Field>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field htmlFor={`${idPrefix}-context-from`} label={t.cron.editor.contextFrom}>
              <textarea
                className={`${FIELD_TEXTAREA} min-h-[64px] text-[12px]`}
                id={`${idPrefix}-context-from`}
                placeholder={t.cron.editor.contextFromPlaceholder}
                value={form.context_from}
                onChange={(event) => update("context_from", event.target.value)}
              />
            </Field>
            <Field label={t.cron.editor.toolsets}>
              <NameCheckboxPicker
                id={`${idPrefix}-toolsets`}
                available={resources.availableToolsets}
                selected={form.enabled_toolsets}
                onChange={(value) => update("enabled_toolsets", value)}
                emptyLabel={t.cron.editor.toolsetsEmpty}
              />
            </Field>
          </div>
        </div>
      </details>
    </>
  );
}

export function CronJobFormFields({
  idPrefix,
  autoFocus,
  form,
  resources,
  onChange,
}: {
  idPrefix: string;
  autoFocus?: boolean;
  form: CronJobEditorState;
  resources: CronJobFormResources;
  onChange: (form: CronJobEditorState) => void;
}) {
  const { t } = useI18n();
  const update = <K extends keyof CronJobEditorState>(
    key: K,
    next: CronJobEditorState[K],
  ) => onChange({ ...form, [key]: next });
  const modes: Array<{ value: CronJobEditorMode; label: string }> = [
    { value: "employee", label: t.cron.editor.modeEmployee },
    { value: "custom", label: t.cron.editor.modeCustom },
  ];

  return (
    <>
      <div className="grid gap-3">
        <Field htmlFor={`${idPrefix}-name`} label={t.cron.nameOptional}>
          <input
            autoFocus={autoFocus}
            className={FIELD_INPUT}
            id={`${idPrefix}-name`}
            placeholder={t.cron.namePlaceholder}
            value={form.name}
            onChange={(event) => update("name", event.target.value)}
          />
        </Field>

        <Field htmlFor={`${idPrefix}-prompt`} label={t.cron.prompt}>
          <textarea
            className={FIELD_TEXTAREA}
            id={`${idPrefix}-prompt`}
            placeholder={t.cron.promptPlaceholder}
            value={form.prompt}
            onChange={(event) => update("prompt", event.target.value)}
          />
        </Field>

        <ScheduleBuilder value={form.scheduleState} onChange={(state) => update("scheduleState", state)} />

        <Field label={t.cron.editor.modeLabel}>
          <div
            aria-label={t.cron.editor.modeLabel}
            className="flex rounded-lg border border-black/[0.08] bg-[#f3f4f6] p-0.5"
            role="group"
          >
            {modes.map((mode) => (
              <button
                aria-pressed={form.mode === mode.value}
                className={`flex-1 rounded-md px-3 py-1.5 text-[12px] transition ${
                  form.mode === mode.value
                    ? "bg-white text-[#202124] shadow-sm"
                    : "text-[#85888e] hover:text-[#202124]"
                }`}
                key={mode.value}
                onClick={() =>
                  onChange({
                    ...form,
                    mode: mode.value,
                    ...(mode.value === "custom"
                      ? { employee_id: "", target_employee_ids: [] }
                      : {}),
                  })
                }
                type="button"
              >
                {mode.label}
              </button>
            ))}
          </div>
        </Field>

        {form.mode === "employee" ? (
          <CronEmployeeFields
            idPrefix={`${idPrefix}-employee-mode`}
            form={form}
            employees={resources.employees}
            onChange={onChange}
          />
        ) : (
          <CronCustomFields
            idPrefix={`${idPrefix}-custom`}
            form={form}
            resources={resources}
            onChange={onChange}
          />
        )}
      </div>
    </>
  );
}
