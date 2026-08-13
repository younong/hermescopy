import { useCallback, useEffect, useState } from "react";
import { Bot, Link2, Plus, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { NameCheckboxPicker } from "@/components/NameCheckboxPicker";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { guiChatTranslations, useI18n } from "@/i18n";
import {
  api,
  type Employee,
  type EmployeeCatalog,
  type EmployeeCollaborationPolicy,
  type EmployeeLifecycleStatus,
  type EmployeePolicy,
  withHermesAssetAuth,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type EmployeeEditor = { mode: "create" } | { mode: "profile"; employee: Employee };
type BindingEditor = { mode: "create" | "credentials"; employee: Employee };

interface EmployeeDraft {
  policy: EmployeePolicy;
}

interface BindingDraft {
  appId: string;
  appSecret: string;
  domain: "feishu" | "lark";
  encryptKey: string;
  verificationToken: string;
}

type EmployeeText = ReturnType<typeof guiChatTranslations>["employees"];

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

export function EmployeeManagementPane({ onEmployeesChanged }: { onEmployeesChanged?(employees: Employee[]): void } = {}) {
  const { t } = useI18n();
  const text = guiChatTranslations(t).employees;
  const common = t.common;
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [catalog, setCatalog] = useState<EmployeeCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [editor, setEditor] = useState<EmployeeEditor | null>(null);
  const [managedEmployeeId, setManagedEmployeeId] = useState<string | null>(null);
  const [bindingEditor, setBindingEditor] = useState<BindingEditor | null>(null);
  const [employeeDraft, setEmployeeDraft] = useState<EmployeeDraft>({ policy: emptyPolicy(null) });
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
  const editorRef = useModalBehavior({ onClose: closeEditor, open: editor !== null });
  const managementRef = useModalBehavior({ onClose: closeManagement, open: managedEmployeeId !== null });
  const bindingEditorRef = useModalBehavior({
    onClose: closeBindingEditor,
    open: bindingEditor !== null,
  });

  useEffect(() => {
    if (!avatarFile) return;
    const url = URL.createObjectURL(avatarFile);
    setAvatarPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [avatarFile]);

  const refreshEmployees = useCallback(async () => {
    const response = await api.getEmployees();
    setEmployees(response.employees);
    setCollaborationDrafts({});
    onEmployeesChanged?.(response.employees);
  }, [onEmployeesChanged]);

  useEffect(() => {
    void Promise.all([refreshEmployees(), api.getEmployeeCatalog().then(setCatalog)])
      .catch((error: unknown) => showToast(text.loadFailed.replace("{error}", String(error)), "error"))
      .finally(() => setLoading(false));
  }, [refreshEmployees, showToast, text.loadFailed]);

  const resetAvatar = (preview: string | null = null) => {
    setAvatarFile(null);
    setAvatarPreview(preview);
    setAvatarRemoved(false);
  };

  const openCreate = () => {
    resetAvatar();
    setEmployeeDraft({ policy: emptyPolicy(catalog) });
    setEditor({ mode: "create" });
  };

  const openProfile = (employee: Employee) => {
    resetAvatar(employee.avatar_url);
    setEmployeeDraft({
      policy: employee.profile
        ? { ...employee.profile, toolsets: allToolsets(catalog) }
        : emptyPolicy(catalog),
    });
    setEditor({ employee, mode: "profile" });
  };

  const openBinding = (employee: Employee) => {
    const binding = employee.channels.feishu;
    setBindingDraft({ ...EMPTY_BINDING, appId: binding?.app_id ?? "" });
    setBindingEditor({ employee, mode: binding ? "credentials" : "create" });
  };

  const saveEmployee = async () => {
    if (!editor) return;
    const policy = employeeDraft.policy;
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

  if (loading) {
    return <div className="flex min-h-48 items-center justify-center"><Spinner /></div>;
  }

  const managedEmployee = employees.find((employee) => employee.employee_id === managedEmployeeId) ?? null;

  return (
    <section data-employee-management-pane className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-5 sm:px-6">
      <Toast toast={toast} />
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-[15px] font-semibold text-[#25282d]">{text.title}</h2>
          <p className="mt-1 text-xs text-[#777c84]">{text.description}</p>
        </div>
        <Button className="gui-chat-workspace-primary-button" onClick={openCreate} size="sm" prefix={<Plus className="h-4 w-4" />}>{text.add}</Button>
      </div>

      {employees.length === 0 ? (
        <div className="rounded-xl border border-dashed border-[#dfe2e7] px-5 py-10 text-center">
          <Bot className="mx-auto h-6 w-6 text-[#969aa1]" />
          <p className="mt-2 text-sm font-medium">{text.none}</p>
          <p className="mt-1 text-xs text-[#969aa1]">{text.emptyHint}</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-[#e1e3e7] bg-white">
          <div aria-hidden className="hidden grid-cols-[minmax(0,1fr)_7rem_9rem_5rem] gap-4 border-b border-[#e1e3e7] bg-[#f8f9fa] px-4 py-2 text-[11px] font-medium text-[#777c84] sm:grid">
            <span>{text.title}</span><span>{text.status}</span><span>{text.channel}</span><span className="text-right">{text.actions}</span>
          </div>
          <ul aria-label={text.listLabel} className="divide-y divide-[#e8eaed]" role="list">
            {employees.map((employee) => (
              <EmployeeListItem employee={employee} key={employee.employee_id} onManage={() => setManagedEmployeeId(employee.employee_id)} text={text} />
            ))}
          </ul>
        </div>
      )}

      {managedEmployee ? (
        <div aria-label={text.manageNamed.replace("{name}", managedEmployee.profile?.name || text.unnamed)} aria-modal="true" className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" ref={managementRef} role="dialog">
          <div className="relative flex max-h-[92vh] w-full max-w-2xl flex-col rounded-xl border border-[#e1e3e7] bg-white shadow-2xl">
            <button aria-label={common.close} className="gui-chat-icon-button absolute right-3 top-3" onClick={closeManagement} type="button"><X /></button>
            <div className="border-b border-[#ebecef] px-5 py-4">
              <h3 className="text-[15px] font-semibold">{managedEmployee.profile?.name || text.unnamed}</h3>
              <p className="mt-1 text-[11px] text-[#969aa1]">{text.manageDescription}</p>
            </div>
            <div className="min-h-0 overflow-y-auto p-5">
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
                onProfile={() => openProfile(managedEmployee)}
                savingLabel={common.saving}
                text={text}
              />
            </div>
          </div>
        </div>
      ) : null}

      {editor ? (
        <div aria-label={editor.mode === "create" ? text.addTitle : text.editTitle} aria-modal="true" className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" ref={editorRef} role="dialog">
          <div className="relative flex max-h-[92vh] w-full max-w-2xl flex-col rounded-xl border border-[#e1e3e7] bg-white shadow-2xl">
            <button aria-label={common.close} className="gui-chat-icon-button absolute right-3 top-3" onClick={closeEditor} type="button"><X /></button>
            <div className="border-b border-[#ebecef] px-5 py-4">
              <h3 className="text-[15px] font-semibold">{editor.mode === "create" ? text.addTitle : text.editTitle}</h3>
              <p className="mt-1 text-[11px] text-[#969aa1]">{text.revisionHint}</p>
            </div>
            <div className="min-h-0 overflow-y-auto p-5">
              <PolicyEditor
                avatarPreview={avatarPreview}
                catalog={catalog}
                onAvatarChange={(file) => { setAvatarFile(file); setAvatarRemoved(false); }}
                onAvatarRemove={() => { setAvatarFile(null); setAvatarPreview(null); setAvatarRemoved(true); }}
                onChange={(policy) => setEmployeeDraft({ policy })}
                policy={employeeDraft.policy}
                text={text}
              />
              <div className="mt-5 flex justify-end gap-2">
                <Button ghost onClick={closeEditor} size="sm">{common.cancel}</Button>
                <Button disabled={busy === "employee:save"} onClick={() => void saveEmployee()} size="sm">{busy === "employee:save" ? common.saving : common.save}</Button>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {bindingEditor ? (
        <div aria-label={text.bindingTitle} aria-modal="true" className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" ref={bindingEditorRef} role="dialog">
          <div className="relative w-full max-w-lg rounded-xl border border-[#e1e3e7] bg-white p-5 shadow-2xl">
            <button aria-label={common.close} className="gui-chat-icon-button absolute right-3 top-3" onClick={closeBindingEditor} type="button"><X /></button>
            <h3 className="text-[15px] font-semibold">{bindingEditor.mode === "create" ? text.connectBinding : text.updateBinding}</h3>
            <p className="mt-1 text-[11px] text-[#969aa1]">{text.bindingHint}</p>
            <div className="mt-4 grid gap-3">
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
          </div>
        </div>
      ) : null}
    </section>
  );
}

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
  return (
    <div className="grid gap-4">
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
      <Field label={text.model}><select className="h-9 rounded-md border border-[#dfe2e7] bg-white px-3 text-sm" value={policy.model_registration_id} onChange={(event) => onChange({ ...policy, model_registration_id: event.target.value })}><option value="">{text.selectModel}</option>{catalog?.model_registrations.map((item) => <option key={item.id} value={item.id}>{item.model || item.name}</option>)}</select></Field>
      <Field label={text.systemPrompt}><textarea className="min-h-28 rounded-md border border-[#dfe2e7] bg-white p-3 text-sm" value={policy.system_prompt} onChange={(event) => onChange({ ...policy, system_prompt: event.target.value })} /></Field>
      <Field label={text.skills}><NameCheckboxPicker available={catalog?.skills ?? []} emptyLabel={text.noSkills} id="employee-skills" onChange={(skills) => onChange({ ...policy, skills })} selected={policy.skills} /></Field>
      <Field label={text.maxIterations}><Input min={1} onChange={(event) => onChange({ ...policy, max_iterations: Number(event.target.value) || 1 })} type="number" value={policy.max_iterations} /></Field>
    </div>
  );
}

function EmployeeListItem({ employee, onManage, text }: { employee: Employee; onManage(): void; text: EmployeeText }) {
  const binding = employee.channels.feishu;
  return (
    <li className="grid min-h-16 items-center gap-3 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_7rem_9rem_5rem] sm:gap-4" role="listitem">
      <div className="flex min-w-0 items-center gap-3">
        <EmployeeAvatar employee={employee} />
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium">{employee.profile?.name || text.unnamed}</h3>
          <p className="truncate text-[11px] text-[#969aa1]">{employee.profile?.role || text.aiEmployee} · {text.profileRevision.replace("{revision}", String(employee.profile_revision ?? "—"))}</p>
        </div>
      </div>
      <div><StatusPill status={employee.lifecycle_status} text={text} /></div>
      <div className="flex items-center gap-2 text-xs text-[#777c84]">
        <Link2 className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate">{binding ? bindingRuntimeLabel(binding.runtime_state, text) : text.notConnected}</span>
      </div>
      <div className="sm:text-right"><Button ghost onClick={onManage} size="sm">{text.manage}</Button></div>
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
