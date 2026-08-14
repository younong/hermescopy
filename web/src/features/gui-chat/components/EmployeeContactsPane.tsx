import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, Bot, Link2, Plus, Search, Settings } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { NameCheckboxPicker } from "@/components/NameCheckboxPicker";
import { guiChatTranslations, useI18n } from "@/i18n";
import {
  api,
  type Employee,
  type EmployeeCatalog,
  type EmployeeCollaborationPolicy,
  type EmployeeLifecycleStatus,
  type EmployeePolicy,
  type ReasoningLevel,
  withHermesAssetAuth,
} from "@/lib/api";
import { REASONING_LEVEL_LABELS } from "@/lib/reasoning-level";
import { cn } from "@/lib/utils";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";

type EmployeeEditor = { mode: "create" } | { mode: "profile"; employee: Employee };
type BindingEditor = { mode: "create" | "credentials"; employee: Employee };

interface BindingDraft {
  appId: string;
  appSecret: string;
  domain: "feishu" | "lark";
  encryptKey: string;
  verificationToken: string;
}

type EmployeeText = ReturnType<typeof guiChatTranslations>["employees"];

const EMPLOYEE_SELECT_CLASS = "h-9 w-full min-w-0 rounded-lg border border-[#dfe2e7] bg-white px-3 text-[13px] text-[#2c2f35] outline-none transition focus:border-[#8ca7c5] focus:ring-2 focus:ring-[#dce5ef]";

function lifecycleLabel(status: EmployeeLifecycleStatus, text: EmployeeText) {
  return text.lifecycle[status];
}

function bindingRuntimeLabel(runtimeState: string, text: EmployeeText) {
  const normalized = runtimeState.toLowerCase() as keyof EmployeeText["runtime"];
  return text.runtime[normalized] ?? text.runtime.unknown;
}

function allToolsets(catalog: EmployeeCatalog | null) {
  return catalog?.toolsets.map((item) => item.name) ?? [];
}

function emptyPolicy(catalog: EmployeeCatalog | null): EmployeePolicy {
  return {
    knowledge_relative_paths: [],
    max_iterations: 20,
    max_tokens: null,
    mcp_servers: [],
    model_registration_id: catalog?.model_registrations[0]?.id ?? "",
    name: "",
    reasoning_effort: "",
    role: "",
    schema_version: 1,
    skills: [],
    system_prompt: "",
    toolsets: allToolsets(catalog),
    workspace_relative_path: "employees/new-employee",
  };
}

const EMPTY_BINDING: BindingDraft = {
  appId: "",
  appSecret: "",
  domain: "feishu",
  encryptKey: "",
  verificationToken: "",
};

export type EmployeeContactsLoadStatus = "loading" | "ready" | "error";

export interface EmployeeContactsPaneProps {
  employees: Employee[];
  loadStatus: EmployeeContactsLoadStatus;
  selectedEmployeeId: string | null;
  onEmployeeSelect(employeeId: string): void;
  onRefresh(): void | Promise<void>;
}

export const EmployeeContactsPane = memo(function EmployeeContactsPane({
  employees,
  loadStatus,
  selectedEmployeeId,
  onEmployeeSelect,
  onRefresh,
}: EmployeeContactsPaneProps) {
  const { t } = useI18n();
  const copy = guiChatTranslations(t);
  const text = copy.employees;
  const common = t.common;
  const [catalog, setCatalog] = useState<EmployeeCatalog | null>(null);
  const [query, setQuery] = useState("");
  const [editor, setEditor] = useState<EmployeeEditor | null>(null);
  const [managedEmployeeId, setManagedEmployeeId] = useState<string | null>(null);
  const [bindingEditor, setBindingEditor] = useState<BindingEditor | null>(null);
  const [employeeDraft, setEmployeeDraft] = useState<EmployeePolicy>(() => emptyPolicy(null));
  const [bindingDraft, setBindingDraft] = useState<BindingDraft>(EMPTY_BINDING);
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [avatarPreview, setAvatarPreview] = useState<string | null>(null);
  const [avatarRemoved, setAvatarRemoved] = useState(false);
  const [collaborationDrafts, setCollaborationDrafts] = useState<Record<string, EmployeeCollaborationPolicy>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const { toast, showToast } = useToast();

  const closeEditor = useCallback(() => setEditor(null), []);
  const closeManagement = useCallback(() => setManagedEmployeeId(null), []);
  const closeBindingEditor = useCallback(() => setBindingEditor(null), []);

  useEffect(() => {
    if (!avatarFile) return;
    const url = URL.createObjectURL(avatarFile);
    setAvatarPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [avatarFile]);

  const refreshEmployees = useCallback(async () => {
    setCollaborationDrafts({});
    await onRefresh();
  }, [onRefresh]);

  const ensureCatalog = useCallback(async () => {
    if (catalog) return catalog;
    try {
      const response = await api.getEmployeeCatalog();
      setCatalog(response);
      return response;
    } catch (error) {
      showToast(text.loadFailed.replace("{error}", String(error)), "error");
      return null;
    }
  }, [catalog, showToast, text.loadFailed]);

  const visibleEmployees = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    if (!normalizedQuery) return employees;
    return employees.filter((employee) =>
      [employee.profile?.name, employee.profile?.role]
        .filter(Boolean)
        .join("\n")
        .toLocaleLowerCase()
        .includes(normalizedQuery),
    );
  }, [employees, query]);

  const resetAvatar = (preview: string | null = null) => {
    setAvatarFile(null);
    setAvatarPreview(preview);
    setAvatarRemoved(false);
  };

  const openCreate = async () => {
    const nextCatalog = await ensureCatalog();
    if (!nextCatalog) return;
    resetAvatar();
    setEmployeeDraft(emptyPolicy(nextCatalog));
    setEditor({ mode: "create" });
  };

  const openProfile = async (employee: Employee) => {
    const nextCatalog = await ensureCatalog();
    if (!nextCatalog) return;
    resetAvatar(employee.avatar_url);
    setEmployeeDraft(employee.profile
      ? { ...employee.profile, toolsets: allToolsets(nextCatalog) }
      : emptyPolicy(nextCatalog));
    setEditor({ employee, mode: "profile" });
  };

  const openBinding = (employee: Employee) => {
    const binding = employee.channels.feishu;
    setBindingDraft({ ...EMPTY_BINDING, appId: binding?.app_id ?? "" });
    setBindingEditor({ employee, mode: binding ? "credentials" : "create" });
  };

  const saveEmployee = async () => {
    if (!editor) return;
    const policy = employeeDraft;
    if (!policy.name?.trim() || !policy.model_registration_id || !policy.system_prompt.trim()) {
      showToast(text.policyRequired, "error");
      return;
    }
    setBusy("employee:save");
    try {
      const saved = editor.mode === "create"
        ? await api.createEmployee({ activate: true, profile: policy })
        : await api.updateEmployeeProfile(editor.employee.employee_id, {
            expected_revision: editor.employee.profile_revision ?? 0,
            profile: policy,
          });
      if (avatarFile) {
        await api.uploadEmployeeAvatar(saved.employee_id, avatarFile);
      } else if (
        editor.mode === "profile"
        && avatarRemoved
        && editor.employee.avatar_url
      ) {
        await api.deleteEmployeeAvatar(saved.employee_id);
      }
      showToast(editor.mode === "create" ? text.created : text.profileSaved, "success");
      closeEditor();
      await refreshEmployees();
    } catch (error) {
      showToast(text.saveFailed.replace("{error}", String(error)), "error");
    } finally {
      setBusy(null);
    }
  };

  const saveBinding = async () => {
    if (!bindingEditor || !bindingDraft.appSecret) {
      showToast(text.appSecretRequired, "error");
      return;
    }
    setBusy("binding:save");
    try {
      const { employee, mode } = bindingEditor;
      if (mode === "create") {
        if (!bindingDraft.appId.trim()) throw new Error(text.appIdRequired);
        await api.createEmployeeFeishuBinding(employee.employee_id, {
          activate: true,
          app_id: bindingDraft.appId.trim(),
          app_secret: bindingDraft.appSecret,
          domain: bindingDraft.domain,
          encrypt_key: bindingDraft.encryptKey || undefined,
          verification_token: bindingDraft.verificationToken || undefined,
        });
      } else {
        const binding = employee.channels.feishu;
        if (!binding) throw new Error(text.bindingUnavailable);
        await api.updateEmployeeFeishuBinding(employee.employee_id, {
          app_secret: bindingDraft.appSecret,
          encrypt_key: bindingDraft.encryptKey || undefined,
          expected_credential_version: binding.credential_version,
          verification_token: bindingDraft.verificationToken || undefined,
        });
      }
      showToast(mode === "create" ? text.bindingConnected : text.bindingUpdated, "success");
      closeBindingEditor();
      await refreshEmployees();
    } catch (error) {
      showToast(text.bindingSaveFailed.replace("{error}", String(error)), "error");
    } finally {
      setBusy(null);
    }
  };

  const saveCollaboration = async (employee: Employee) => {
    const draft = collaborationDrafts[employee.employee_id] ?? employee.collaboration_policy;
    setBusy(`${employee.employee_id}:collaboration`);
    try {
      await api.updateEmployeeCollaborationPolicy(employee.employee_id, draft);
      showToast(text.permissionsSaved, "success");
      await refreshEmployees();
    } catch (error) {
      showToast(text.permissionsSaveFailed.replace("{error}", String(error)), "error");
    } finally {
      setBusy(null);
    }
  };

  const runEmployeeAction = async (
    employee: Employee,
    action: EmployeeLifecycleStatus | "rollover",
  ) => {
    setBusy(`${employee.employee_id}:${action}`);
    try {
      if (action === "rollover") {
        const result = await api.rolloverEmployeeSessions(employee.employee_id);
        showToast(text.sessionsRefreshed.replace("{count}", String(result.retired_sessions)), "success");
      } else {
        await api.updateEmployeeLifecycle(employee.employee_id, action);
        showToast(text.lifecycleUpdated.replace("{status}", lifecycleLabel(action, text)), "success");
      }
      await refreshEmployees();
    } catch (error) {
      showToast(text.actionFailed.replace("{error}", String(error)), "error");
    } finally {
      setBusy(null);
    }
  };

  const runBindingAction = async (
    employee: Employee,
    action: "test" | EmployeeLifecycleStatus,
  ) => {
    setBusy(`${employee.employee_id}:binding:${action}`);
    try {
      if (action === "test") {
        const result = await api.testEmployeeFeishuBinding(employee.employee_id);
        showToast(
          result.ok
            ? result.bot_name
              ? text.connectionSucceededNamed.replace("{name}", result.bot_name)
              : text.connectionSucceeded
            : text.connectionFailed,
          result.ok ? "success" : "error",
        );
      } else {
        await api.updateEmployeeFeishuBindingLifecycle(employee.employee_id, action);
        showToast(text.bindingLifecycleUpdated.replace("{status}", lifecycleLabel(action, text)), "success");
      }
      await refreshEmployees();
    } catch (error) {
      showToast(text.bindingActionFailed.replace("{error}", String(error)), "error");
    } finally {
      setBusy(null);
    }
  };

  const managedEmployee = employees.find((employee) => employee.employee_id === managedEmployeeId) ?? null;

  return (
    <section
      aria-label={copy.shell.contacts}
      className="flex h-full min-h-0 w-full min-w-0 flex-col overflow-hidden bg-[#f7f7f8]"
      data-employee-contacts-pane
    >
      <Toast toast={toast} />
      <header className="flex items-center justify-between gap-2 border-b border-black/[0.06] px-3 py-3">
        <div className="min-w-0">
          <h2 className="truncate text-[14px] font-semibold text-[#202124]">{copy.shell.contacts}</h2>
          <p className="truncate text-[11px] text-[#85888e]">{copy.shell.selectContact}</p>
        </div>
        <Button
          aria-label={text.add}
          className="h-8 w-8 shrink-0 rounded-lg"
          ghost
          onClick={() => void openCreate()}
          size="icon"
          title={text.add}
        >
          <Plus className="h-4 w-4" />
        </Button>
      </header>

      <div className="px-3 py-2.5">
        <label className="flex h-9 items-center gap-2 rounded-lg border border-black/[0.08] bg-white px-3 text-[#85888e] focus-within:border-black/20">
          <Search aria-hidden className="h-3.5 w-3.5 shrink-0" />
          <input
            aria-label={common.search}
            className="min-w-0 flex-1 bg-transparent text-[13px] text-[#202124] outline-none placeholder:text-[#a0a3a8]"
            onChange={(event) => setQuery(event.target.value)}
            placeholder={common.search}
            value={query}
          />
        </label>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {loadStatus === "loading" ? (
          <div className="flex items-center justify-center gap-2 px-3 py-10 text-xs text-[#85888e]" role="status">
            <Spinner /> {common.loading}
          </div>
        ) : loadStatus === "error" ? (
          <div className="flex flex-col items-center gap-3 px-4 py-10 text-center text-xs" role="alert">
            <AlertCircle className="h-5 w-5 text-[#a8322d]" />
            <span className="text-[#777c84]">{text.loadFailed.replace("{error}", "").replace(/[:：]\s*$/, "")}</span>
            <Button onClick={() => void onRefresh()} outlined size="sm">{common.retry}</Button>
          </div>
        ) : employees.length === 0 ? (
          <div className="px-4 py-10 text-center text-xs text-[#85888e]">
            <Bot className="mx-auto mb-2 h-5 w-5" />
            <strong className="block font-medium text-[#4d5055]">{text.none}</strong>
            <span className="mt-1 block">{text.emptyHint}</span>
          </div>
        ) : visibleEmployees.length === 0 ? (
          <div className="px-4 py-10 text-center text-xs text-[#85888e]">{common.noResults}</div>
        ) : (
          <ul aria-label={text.listLabel} className="flex flex-col gap-1" role="list">
            {visibleEmployees.map((employee) => (
              <EmployeeContactRow
                employee={employee}
                key={employee.employee_id}
                onManage={() => setManagedEmployeeId(employee.employee_id)}
                onSelect={() => onEmployeeSelect(employee.employee_id)}
                selected={employee.employee_id === selectedEmployeeId}
                text={text}
              />
            ))}
          </ul>
        )}
      </div>

      {managedEmployee ? (
        <GuiChatWorkspaceDialog
          busy={busy !== null}
          description={text.manageDescription}
          onClose={closeManagement}
          title={managedEmployee.profile?.name || text.unnamed}
          wide
        >
          <div className="max-h-[72vh] overflow-y-auto">
            <EmployeeManagementDetails
              busy={busy}
              collaborationPolicy={collaborationDrafts[managedEmployee.employee_id] ?? managedEmployee.collaboration_policy}
              employee={managedEmployee}
              onBinding={() => openBinding(managedEmployee)}
              onBindingAction={(action) => void runBindingAction(managedEmployee, action)}
              onCollaborationChange={(policy) => setCollaborationDrafts((current) => ({
                ...current,
                [managedEmployee.employee_id]: policy,
              }))}
              onCollaborationSave={() => void saveCollaboration(managedEmployee)}
              onEmployeeAction={(action) => void runEmployeeAction(managedEmployee, action)}
              onProfile={() => void openProfile(managedEmployee)}
              savingLabel={common.saving}
              text={text}
            />
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}

      {editor ? (
        <GuiChatWorkspaceDialog
          busy={busy === "employee:save"}
          description={text.revisionHint}
          onClose={closeEditor}
          title={editor.mode === "create" ? text.addTitle : text.editTitle}
          wide
        >
          <div className="max-h-[72vh] overflow-y-auto">
            <PolicyEditor
              avatarPreview={avatarPreview}
              catalog={catalog}
              onAvatarChange={(file) => { setAvatarFile(file); setAvatarRemoved(false); }}
              onAvatarRemove={() => { setAvatarFile(null); setAvatarPreview(null); setAvatarRemoved(true); }}
              onChange={setEmployeeDraft}
              policy={employeeDraft}
              text={text}
            />
            <div className="mt-5 flex justify-end gap-2">
              <Button ghost onClick={closeEditor} size="sm">{common.cancel}</Button>
              <Button disabled={busy === "employee:save"} onClick={() => void saveEmployee()} size="sm">{busy === "employee:save" ? common.saving : common.save}</Button>
            </div>
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}

      {bindingEditor ? (
        <GuiChatWorkspaceDialog
          busy={busy === "binding:save"}
          description={text.bindingHint}
          onClose={closeBindingEditor}
          title={bindingEditor.mode === "create" ? text.connectBinding : text.updateBinding}
        >
          <div className="grid gap-3">
            {bindingEditor.mode === "create" ? (
              <>
                <Field label={text.appId}><Input value={bindingDraft.appId} onChange={(event) => setBindingDraft((current) => ({ ...current, appId: event.target.value }))} /></Field>
                <Field label={text.platform}><select className="h-9 rounded-md border border-[#dfe2e7] bg-white px-3 text-sm" value={bindingDraft.domain} onChange={(event) => setBindingDraft((current) => ({ ...current, domain: event.target.value as "feishu" | "lark" }))}><option value="feishu">{text.feishuPlatform}</option><option value="lark">Lark</option></select></Field>
              </>
            ) : null}
            <Field label={bindingEditor.mode === "create" ? text.appSecret : text.newAppSecret}><Input type="password" value={bindingDraft.appSecret} onChange={(event) => setBindingDraft((current) => ({ ...current, appSecret: event.target.value }))} /></Field>
            <Field label={text.encryptKey}><Input type="password" value={bindingDraft.encryptKey} onChange={(event) => setBindingDraft((current) => ({ ...current, encryptKey: event.target.value }))} /></Field>
            <Field label={text.verificationToken}><Input type="password" value={bindingDraft.verificationToken} onChange={(event) => setBindingDraft((current) => ({ ...current, verificationToken: event.target.value }))} /></Field>
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <Button ghost onClick={closeBindingEditor} size="sm">{common.cancel}</Button>
            <Button disabled={busy === "binding:save"} onClick={() => void saveBinding()} size="sm">{busy === "binding:save" ? common.saving : common.save}</Button>
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}
    </section>
  );
});

function Field({ children, label }: { children: React.ReactNode; label: string }) {
  return <div className="grid gap-1.5"><Label>{label}</Label>{children}</div>;
}

function PolicyEditor({
  avatarPreview,
  catalog,
  onAvatarChange,
  onAvatarRemove,
  onChange,
  policy,
  text,
}: {
  avatarPreview: string | null;
  catalog: EmployeeCatalog | null;
  onAvatarChange(file: File): void;
  onAvatarRemove(): void;
  onChange(policy: EmployeePolicy): void;
  policy: EmployeePolicy;
  text: EmployeeText;
}) {
  const selectedModel = catalog?.model_registrations.find(
    (item) => item.id === policy.model_registration_id,
  );
  const availableReasoningLevels = selectedModel?.reasoning_levels ?? [];
  return (
    <div className="gui-chat-employee-policy-editor grid gap-4">
      <Field label={text.avatar}>
        <div className="flex items-center gap-3">
          <EmployeeAvatar employee={{ avatar_url: avatarPreview, profile: policy }} large />
          <div className="flex gap-2">
            <label className="inline-flex h-8 cursor-pointer items-center rounded-md border border-[#dfe2e7] px-3 text-xs hover:bg-[#f6f7f9]"><input accept="image/jpeg,image/png,image/webp" className="hidden" onChange={(event) => { const file = event.currentTarget.files?.[0]; if (file) onAvatarChange(file); event.currentTarget.value = ""; }} type="file" />{text.chooseImage}</label>
            {avatarPreview ? <Button ghost onClick={onAvatarRemove} size="sm">{text.removeImage}</Button> : null}
          </div>
        </div>
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={text.name}><Input value={policy.name ?? ""} onChange={(event) => onChange({ ...policy, name: event.target.value })} /></Field>
        <Field label={text.role}><Input value={policy.role ?? ""} onChange={(event) => onChange({ ...policy, role: event.target.value })} /></Field>
      </div>
      <div
        className={cn(
          "grid gap-3",
          availableReasoningLevels.length > 0 && "sm:grid-cols-[minmax(0,1fr)_minmax(9rem,0.42fr)]",
        )}
        data-employee-model-reasoning-row
      >
        <Field label={text.model}>
          <select
            className={EMPLOYEE_SELECT_CLASS}
            onChange={(event) => onChange({
              ...policy,
              model_registration_id: event.target.value,
              reasoning_effort: "",
            })}
            value={policy.model_registration_id}
          >
            <option value="">{text.selectModel}</option>
            {catalog?.model_registrations.map((item) => (
              <option key={item.id} value={item.id}>{item.model || item.name}</option>
            ))}
          </select>
        </Field>
        {availableReasoningLevels.length > 0 ? (
          <Field label={text.reasoningEffort}>
            <select
              className={EMPLOYEE_SELECT_CLASS}
              onChange={(event) => onChange({
                ...policy,
                reasoning_effort: event.target.value as ReasoningLevel | "",
              })}
              value={policy.reasoning_effort ?? ""}
            >
              <option value="">{text.reasoningDefault}</option>
              {availableReasoningLevels.map((level) => (
                <option key={level} value={level}>{REASONING_LEVEL_LABELS[level]}</option>
              ))}
            </select>
          </Field>
        ) : null}
      </div>
      <Field label={text.systemPrompt}><textarea className="min-h-28 rounded-md border border-[#dfe2e7] bg-white p-3 text-sm" value={policy.system_prompt} onChange={(event) => onChange({ ...policy, system_prompt: event.target.value })} /></Field>
      <Field label={text.skills}><NameCheckboxPicker available={catalog?.skills ?? []} emptyLabel={text.noSkills} id="employee-skills" onChange={(skills) => onChange({ ...policy, skills })} selected={policy.skills} /></Field>
      <Field label={text.maxIterations}><Input min={1} onChange={(event) => onChange({ ...policy, max_iterations: Number(event.target.value) || 1 })} type="number" value={policy.max_iterations} /></Field>
    </div>
  );
}

function EmployeeContactRow({
  employee,
  onManage,
  onSelect,
  selected,
  text,
}: {
  employee: Employee;
  onManage(): void;
  onSelect(): void;
  selected: boolean;
  text: EmployeeText;
}) {
  const unavailable = employee.lifecycle_status !== "active";
  const name = employee.profile?.name || text.unnamed;
  return (
    <li className="group relative" role="listitem">
      <button
        aria-current={selected ? "true" : undefined}
        aria-disabled={unavailable || undefined}
        className={cn(
          "flex min-h-14 w-full items-center gap-3 rounded-[10px] px-2.5 py-2 pr-11 text-left transition-colors",
          selected ? "bg-white text-black shadow-sm" : "text-[#33363b] hover:bg-black/[0.04]",
          unavailable && "cursor-not-allowed opacity-55",
        )}
        onClick={() => {
          if (!unavailable) onSelect();
        }}
        type="button"
      >
        <EmployeeAvatar employee={employee} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[14px] font-medium leading-5">{name}</span>
          <span className="flex items-center gap-1.5 truncate text-[11px] leading-4 text-[#85888e]">
            <span className="truncate">{employee.profile?.role || text.aiEmployee}</span>
            {unavailable ? <StatusPill status={employee.lifecycle_status} text={text} /> : null}
          </span>
        </span>
      </button>
      <button
        aria-label={text.manageNamed.replace("{name}", name)}
        className="absolute right-2 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-[#85888e] opacity-70 hover:bg-black/[0.06] hover:text-[#33363b] focus:opacity-100 group-hover:opacity-100"
        onClick={(event) => {
          event.stopPropagation();
          onManage();
        }}
        title={text.manage}
        type="button"
      >
        <Settings aria-hidden className="h-3.5 w-3.5" />
      </button>
    </li>
  );
}

function EmployeeManagementDetails({
  busy,
  collaborationPolicy,
  employee,
  onBinding,
  onBindingAction,
  onCollaborationChange,
  onCollaborationSave,
  onEmployeeAction,
  onProfile,
  savingLabel,
  text,
}: {
  busy: string | null;
  collaborationPolicy: EmployeeCollaborationPolicy;
  employee: Employee;
  onBinding(): void;
  onBindingAction(action: "test" | EmployeeLifecycleStatus): void;
  onCollaborationChange(policy: EmployeeCollaborationPolicy): void;
  onCollaborationSave(): void;
  onEmployeeAction(action: "rollover" | EmployeeLifecycleStatus): void;
  onProfile(): void;
  savingLabel: string;
  text: EmployeeText;
}) {
  const binding = employee.channels.feishu;
  const disabled = busy?.startsWith(`${employee.employee_id}:`) ?? false;
  const unlimited = collaborationPolicy.invite_quota === null;
  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <EmployeeAvatar employee={employee} />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2"><h3 className="truncate text-sm font-semibold">{employee.profile?.name || text.unnamed}</h3><StatusPill status={employee.lifecycle_status} text={text} /></div>
            <p className="truncate text-[11px] text-[#969aa1]">{employee.profile?.role || text.aiEmployee} · {text.profileRevision.replace("{revision}", String(employee.profile_revision ?? "—"))}</p>
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Button disabled={disabled || employee.lifecycle_status === "revoked"} ghost onClick={onProfile} size="sm">{text.editProfile}</Button>
          <Button disabled={disabled || employee.lifecycle_status !== "active"} ghost onClick={() => onEmployeeAction("rollover")} size="sm">{text.refreshSessions}</Button>
          {employee.lifecycle_status === "active" ? <Button disabled={disabled} ghost onClick={() => onEmployeeAction("suspended")} size="sm">{text.suspend}</Button> : employee.lifecycle_status === "suspended" ? <Button disabled={disabled} ghost onClick={() => onEmployeeAction("active")} size="sm">{text.resume}</Button> : null}
          <Button disabled={disabled || employee.lifecycle_status === "revoked"} ghost onClick={() => onEmployeeAction("revoked")} size="sm">{text.revoke}</Button>
        </div>
      </div>

      <div className="mt-4 grid gap-3 border-l-2 border-[#e8eaed] pl-3 sm:grid-cols-2">
        <label className="flex items-center justify-between gap-3 text-xs"><span><span className="block font-medium">{text.allowCollaboration}</span><span className="text-[#969aa1]">{text.allowCollaborationHint}</span></span><Switch checked={collaborationPolicy.may_participate} className="gui-chat-skill-switch" onCheckedChange={(checked) => onCollaborationChange({ ...collaborationPolicy, may_participate: checked })} /></label>
        <label className="flex items-center justify-between gap-3 text-xs"><span><span className="block font-medium">{text.allowCreateGroups}</span><span className="text-[#969aa1]">{text.allowCreateGroupsHint}</span></span><Switch checked={collaborationPolicy.may_create_groups} className="gui-chat-skill-switch" onCheckedChange={(checked) => onCollaborationChange({ ...collaborationPolicy, may_create_groups: checked })} /></label>
        <Field label={text.inviteQuota}><Input aria-label={text.inviteQuotaFor.replace("{name}", employee.profile?.name || employee.employee_id)} disabled={unlimited} min={0} onChange={(event) => onCollaborationChange({ ...collaborationPolicy, invite_quota: Math.max(0, Number(event.target.value) || 0) })} type="number" value={collaborationPolicy.invite_quota ?? ""} /></Field>
        <div className="flex items-end justify-between gap-3"><label className="flex items-center gap-2 pb-2 text-xs"><input checked={unlimited} onChange={(event) => onCollaborationChange({ ...collaborationPolicy, invite_quota: event.target.checked ? null : 5 })} type="checkbox" />{text.unlimited}</label><Button disabled={disabled} onClick={onCollaborationSave} size="sm">{busy === `${employee.employee_id}:collaboration` ? savingLabel : text.savePermissions}</Button></div>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-[#ebecef] pt-3">
        <div className="flex items-center gap-2 text-xs"><Link2 className="h-3.5 w-3.5 text-[#777c84]" /><span className="font-medium">{text.channel}</span>{binding ? <><StatusPill status={binding.lifecycle_status} text={text} /><span className="text-[#969aa1]">{bindingRuntimeLabel(binding.runtime_state, text)}</span></> : <span className="text-[#969aa1]">{text.notConnected}</span>}</div>
        <div className="flex flex-wrap gap-1.5">
          {binding ? <><Button disabled={disabled || binding.lifecycle_status === "revoked"} ghost onClick={() => onBindingAction("test")} size="sm">{text.testConnection}</Button><Button disabled={disabled || binding.lifecycle_status === "revoked"} ghost onClick={onBinding} size="sm">{text.updateCredentials}</Button>{binding.lifecycle_status === "active" ? <Button disabled={disabled} ghost onClick={() => onBindingAction("suspended")} size="sm">{text.suspendBinding}</Button> : binding.lifecycle_status === "suspended" ? <Button disabled={disabled} ghost onClick={() => onBindingAction("active")} size="sm">{text.resumeBinding}</Button> : null}<Button disabled={disabled || binding.lifecycle_status === "revoked"} ghost onClick={() => onBindingAction("revoked")} size="sm">{text.revokeBinding}</Button></> : <Button disabled={employee.lifecycle_status === "revoked"} ghost onClick={onBinding} size="sm">{text.connect}</Button>}
        </div>
      </div>
    </div>
  );
}

function EmployeeAvatar({ employee, large = false }: { employee: Pick<Employee, "avatar_url" | "profile">; large?: boolean }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [employee.avatar_url]);
  const label = employee.profile?.name || "E";
  const classes = large ? "h-14 w-14" : "h-10 w-10";
  return employee.avatar_url && !failed
    ? <img alt="" className={cn("shrink-0 rounded-full border border-[#e1e3e7] object-cover", classes)} onError={() => setFailed(true)} src={withHermesAssetAuth(employee.avatar_url)} />
    : <span aria-hidden className={cn("flex shrink-0 items-center justify-center rounded-full border border-[#e1e3e7] bg-[#f3f4f6] text-sm font-semibold", classes)}>{label.trim().charAt(0).toUpperCase() || "E"}</span>;
}

function StatusPill({ status, text }: { status: EmployeeLifecycleStatus; text: EmployeeText }) {
  return <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-medium", status === "active" ? "bg-[#eaf7ef] text-[#237a48]" : status === "suspended" ? "bg-[#fff4dd] text-[#8a5a00]" : "bg-[#fcebea] text-[#a8322d]")}>{lifecycleLabel(status, text)}</span>;
}
