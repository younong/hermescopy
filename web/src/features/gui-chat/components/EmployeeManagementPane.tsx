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
import {
  api,
  type Employee,
  type EmployeeCatalog,
  type EmployeeCollaborationPolicy,
  type EmployeeLifecycleStatus,
  type EmployeePolicy,
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

const LIFECYCLE_LABELS: Record<EmployeeLifecycleStatus, string> = {
  active: "Active",
  revoked: "Revoked",
  suspended: "Suspended",
};

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

export function EmployeeManagementPane() {
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [catalog, setCatalog] = useState<EmployeeCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [editor, setEditor] = useState<EmployeeEditor | null>(null);
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
  const closeBindingEditor = useCallback(() => setBindingEditor(null), []);
  const editorRef = useModalBehavior({ onClose: closeEditor, open: editor !== null });
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
  }, []);

  useEffect(() => {
    void Promise.all([refreshEmployees(), api.getEmployeeCatalog().then(setCatalog)])
      .catch((error: unknown) => showToast(`Could not load employees: ${String(error)}`, "error"))
      .finally(() => setLoading(false));
  }, [refreshEmployees, showToast]);

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
      showToast("Enter a name, select a model, and add a system prompt.", "error");
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
      showToast(editor.mode === "create" ? "Employee created" : "Employee profile saved", "success");
      closeEditor();
      await refreshEmployees();
    } catch (error) {
      showToast(`Could not save employee: ${String(error)}`, "error");
    } finally {
      setBusy(null);
    }
  };

  const saveBinding = async () => {
    if (!bindingEditor || !bindingDraft.appSecret) {
      showToast("App Secret is required.", "error");
      return;
    }
    setBusy("binding:save");
    try {
      const { employee, mode } = bindingEditor;
      if (mode === "create") {
        if (!bindingDraft.appId.trim()) throw new Error("App ID is required");
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
        if (!binding) throw new Error("Feishu binding is no longer available");
        await api.updateEmployeeFeishuBinding(employee.employee_id, {
          app_secret: bindingDraft.appSecret,
          encrypt_key: bindingDraft.encryptKey || undefined,
          expected_credential_version: binding.credential_version,
          verification_token: bindingDraft.verificationToken || undefined,
        });
      }
      showToast(mode === "create" ? "Feishu / Lark connected" : "Binding credentials updated", "success");
      closeBindingEditor();
      await refreshEmployees();
    } catch (error) {
      showToast(`Could not save binding: ${String(error)}`, "error");
    } finally {
      setBusy(null);
    }
  };

  const saveCollaboration = async (employee: Employee) => {
    const draft = collaborationDrafts[employee.employee_id] ?? employee.collaboration_policy;
    setBusy(`${employee.employee_id}:collaboration`);
    try {
      await api.updateEmployeeCollaborationPolicy(employee.employee_id, draft);
      showToast("Collaboration policy saved", "success");
      await refreshEmployees();
    } catch (error) {
      showToast(`Could not save collaboration policy: ${String(error)}`, "error");
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
        showToast(`${result.retired_sessions} conversation session(s) rolled over`, "success");
      } else {
        await api.updateEmployeeLifecycle(employee.employee_id, action);
        showToast(`Employee ${action}`, "success");
      }
      await refreshEmployees();
    } catch (error) {
      showToast(`Employee action failed: ${String(error)}`, "error");
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
          result.ok ? `Connected${result.bot_name ? ` as ${result.bot_name}` : ""}` : "Connection test failed",
          result.ok ? "success" : "error",
        );
      } else {
        await api.updateEmployeeFeishuBindingLifecycle(employee.employee_id, action);
        showToast(`Feishu / Lark binding ${action}`, "success");
      }
      await refreshEmployees();
    } catch (error) {
      showToast(`Binding action failed: ${String(error)}`, "error");
    } finally {
      setBusy(null);
    }
  };

  if (loading) {
    return <div className="flex min-h-48 items-center justify-center"><Spinner /></div>;
  }

  return (
    <section data-employee-management-pane className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-5 sm:px-6">
      <Toast toast={toast} />
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-[15px] font-semibold text-[#25282d]">Employees</h2>
          <p className="mt-1 text-xs text-[#777c84]">Create focused assistants for direct chats and collaboration.</p>
        </div>
        <Button className="gui-chat-workspace-primary-button" onClick={openCreate} size="sm" prefix={<Plus className="h-4 w-4" />}>Add employee</Button>
      </div>

      {employees.length === 0 ? (
        <div className="rounded-xl border border-dashed border-[#dfe2e7] px-5 py-10 text-center">
          <Bot className="mx-auto h-6 w-6 text-[#969aa1]" />
          <p className="mt-2 text-sm font-medium">No employees yet</p>
          <p className="mt-1 text-xs text-[#969aa1]">Add one without connecting a messaging channel.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {employees.map((employee) => (
            <EmployeeRow
              busy={busy}
              collaborationPolicy={collaborationDrafts[employee.employee_id] ?? employee.collaboration_policy}
              employee={employee}
              key={employee.employee_id}
              onBinding={() => openBinding(employee)}
              onBindingAction={(action) => void runBindingAction(employee, action)}
              onCollaborationChange={(policy) => setCollaborationDrafts((current) => ({
                ...current,
                [employee.employee_id]: policy,
              }))}
              onCollaborationSave={() => void saveCollaboration(employee)}
              onEmployeeAction={(action) => void runEmployeeAction(employee, action)}
              onProfile={() => openProfile(employee)}
            />
          ))}
        </div>
      )}

      {editor ? (
        <div aria-label={editor.mode === "create" ? "Add employee" : "Edit employee"} aria-modal="true" className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" ref={editorRef} role="dialog">
          <div className="relative flex max-h-[92vh] w-full max-w-2xl flex-col rounded-xl border border-[#e1e3e7] bg-white shadow-2xl">
            <button aria-label="Close" className="gui-chat-icon-button absolute right-3 top-3" onClick={closeEditor} type="button"><X /></button>
            <div className="border-b border-[#ebecef] px-5 py-4">
              <h3 className="text-[15px] font-semibold">{editor.mode === "create" ? "Add employee" : "Edit employee"}</h3>
              <p className="mt-1 text-[11px] text-[#969aa1]">Profile updates apply to new sessions. Roll over existing sessions when you want them to use the latest revision.</p>
            </div>
            <div className="min-h-0 overflow-y-auto p-5">
              <PolicyEditor
                avatarPreview={avatarPreview}
                catalog={catalog}
                onAvatarChange={(file) => { setAvatarFile(file); setAvatarRemoved(false); }}
                onAvatarRemove={() => { setAvatarFile(null); setAvatarPreview(null); setAvatarRemoved(true); }}
                onChange={(policy) => setEmployeeDraft({ policy })}
                policy={employeeDraft.policy}
              />
              <div className="mt-5 flex justify-end gap-2">
                <Button ghost onClick={closeEditor} size="sm">Cancel</Button>
                <Button disabled={busy === "employee:save"} onClick={() => void saveEmployee()} size="sm">{busy === "employee:save" ? "Saving…" : "Save"}</Button>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {bindingEditor ? (
        <div aria-label="Feishu / Lark binding" aria-modal="true" className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" ref={bindingEditorRef} role="dialog">
          <div className="relative w-full max-w-lg rounded-xl border border-[#e1e3e7] bg-white p-5 shadow-2xl">
            <button aria-label="Close" className="gui-chat-icon-button absolute right-3 top-3" onClick={closeBindingEditor} type="button"><X /></button>
            <h3 className="text-[15px] font-semibold">{bindingEditor.mode === "create" ? "Connect Feishu / Lark" : "Update Feishu / Lark credentials"}</h3>
            <p className="mt-1 text-[11px] text-[#969aa1]">Optional. Hermes supports one Feishu or Lark app binding per employee.</p>
            <div className="mt-4 grid gap-3">
              {bindingEditor.mode === "create" ? (
                <>
                  <Field label="App ID"><Input value={bindingDraft.appId} onChange={(event) => setBindingDraft((current) => ({ ...current, appId: event.target.value }))} /></Field>
                  <Field label="Domain"><select className="h-9 rounded-md border border-[#dfe2e7] bg-white px-3 text-sm" value={bindingDraft.domain} onChange={(event) => setBindingDraft((current) => ({ ...current, domain: event.target.value as "feishu" | "lark" }))}><option value="feishu">Feishu</option><option value="lark">Lark</option></select></Field>
                </>
              ) : null}
              <Field label={bindingEditor.mode === "create" ? "App Secret" : "New App Secret"}><Input type="password" value={bindingDraft.appSecret} onChange={(event) => setBindingDraft((current) => ({ ...current, appSecret: event.target.value }))} /></Field>
              <Field label="Encrypt Key (optional)"><Input type="password" value={bindingDraft.encryptKey} onChange={(event) => setBindingDraft((current) => ({ ...current, encryptKey: event.target.value }))} /></Field>
              <Field label="Verification Token (optional)"><Input type="password" value={bindingDraft.verificationToken} onChange={(event) => setBindingDraft((current) => ({ ...current, verificationToken: event.target.value }))} /></Field>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <Button ghost onClick={closeBindingEditor} size="sm">Cancel</Button>
              <Button disabled={busy === "binding:save"} onClick={() => void saveBinding()} size="sm">{busy === "binding:save" ? "Saving…" : "Save"}</Button>
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
}: {
  avatarPreview: string | null;
  catalog: EmployeeCatalog | null;
  onAvatarChange(file: File): void;
  onAvatarRemove(): void;
  onChange(policy: EmployeePolicy): void;
  policy: EmployeePolicy;
}) {
  return (
    <div className="grid gap-4">
      <Field label="Avatar">
        <div className="flex items-center gap-3">
          <EmployeeAvatar employee={{ avatar_url: avatarPreview, profile: policy }} large />
          <div className="flex gap-2">
            <label className="inline-flex h-8 cursor-pointer items-center rounded-md border border-[#dfe2e7] px-3 text-xs hover:bg-[#f6f7f9]"><input accept="image/jpeg,image/png,image/webp" className="hidden" onChange={(event) => { const file = event.currentTarget.files?.[0]; if (file) onAvatarChange(file); event.currentTarget.value = ""; }} type="file" />Choose image</label>
            {avatarPreview ? <Button ghost onClick={onAvatarRemove} size="sm">Remove</Button> : null}
          </div>
        </div>
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Name"><Input value={policy.name ?? ""} onChange={(event) => onChange({ ...policy, name: event.target.value })} /></Field>
        <Field label="Role"><Input value={policy.role ?? ""} onChange={(event) => onChange({ ...policy, role: event.target.value })} /></Field>
      </div>
      <Field label="Model registration"><select className="h-9 rounded-md border border-[#dfe2e7] bg-white px-3 text-sm" value={policy.model_registration_id} onChange={(event) => onChange({ ...policy, model_registration_id: event.target.value })}><option value="">Select a model</option>{catalog?.model_registrations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
      <Field label="System prompt"><textarea className="min-h-28 rounded-md border border-[#dfe2e7] bg-white p-3 text-sm" value={policy.system_prompt} onChange={(event) => onChange({ ...policy, system_prompt: event.target.value })} /></Field>
      <Field label="Skills"><NameCheckboxPicker available={catalog?.skills ?? []} emptyLabel="No skills available." id="employee-skills" onChange={(skills) => onChange({ ...policy, skills })} selected={policy.skills} /></Field>
      <Field label="Max iterations"><Input min={1} onChange={(event) => onChange({ ...policy, max_iterations: Number(event.target.value) || 1 })} type="number" value={policy.max_iterations} /></Field>
    </div>
  );
}

function EmployeeRow({
  busy,
  collaborationPolicy,
  employee,
  onBinding,
  onBindingAction,
  onCollaborationChange,
  onCollaborationSave,
  onEmployeeAction,
  onProfile,
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
}) {
  const binding = employee.channels.feishu;
  const disabled = busy?.startsWith(`${employee.employee_id}:`) ?? false;
  const unlimited = collaborationPolicy.invite_quota === null;
  return (
    <article className="rounded-xl border border-[#e1e3e7] bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <EmployeeAvatar employee={employee} />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2"><h3 className="truncate text-sm font-semibold">{employee.profile?.name || "Unnamed employee"}</h3><StatusPill status={employee.lifecycle_status} /></div>
            <p className="truncate text-[11px] text-[#969aa1]">{employee.profile?.role || "AI employee"} · profile r{employee.profile_revision ?? "—"}</p>
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Button disabled={disabled || employee.lifecycle_status === "revoked"} ghost onClick={onProfile} size="sm">Edit profile</Button>
          <Button disabled={disabled || employee.lifecycle_status !== "active"} ghost onClick={() => onEmployeeAction("rollover")} size="sm">Roll over sessions</Button>
          {employee.lifecycle_status === "active" ? <Button disabled={disabled} ghost onClick={() => onEmployeeAction("suspended")} size="sm">Suspend</Button> : employee.lifecycle_status === "suspended" ? <Button disabled={disabled} ghost onClick={() => onEmployeeAction("active")} size="sm">Resume</Button> : null}
          <Button disabled={disabled || employee.lifecycle_status === "revoked"} ghost onClick={() => onEmployeeAction("revoked")} size="sm">Revoke</Button>
        </div>
      </div>

      <div className="mt-4 grid gap-3 rounded-lg bg-[#f8f9fa] p-3 sm:grid-cols-2">
        <label className="flex items-center justify-between gap-3 text-xs"><span><span className="block font-medium">May participate</span><span className="text-[#969aa1]">Can join internal groups</span></span><Switch checked={collaborationPolicy.may_participate} className="gui-chat-skill-switch" onCheckedChange={(checked) => onCollaborationChange({ ...collaborationPolicy, may_participate: checked })} /></label>
        <label className="flex items-center justify-between gap-3 text-xs"><span><span className="block font-medium">May create groups</span><span className="text-[#969aa1]">Can invite other employees</span></span><Switch checked={collaborationPolicy.may_create_groups} className="gui-chat-skill-switch" onCheckedChange={(checked) => onCollaborationChange({ ...collaborationPolicy, may_create_groups: checked })} /></label>
        <Field label="Invite quota"><Input aria-label={`Invite quota for ${employee.profile?.name || employee.employee_id}`} disabled={unlimited} min={0} onChange={(event) => onCollaborationChange({ ...collaborationPolicy, invite_quota: Math.max(0, Number(event.target.value) || 0) })} type="number" value={collaborationPolicy.invite_quota ?? ""} /></Field>
        <div className="flex items-end justify-between gap-3"><label className="flex items-center gap-2 pb-2 text-xs"><input checked={unlimited} onChange={(event) => onCollaborationChange({ ...collaborationPolicy, invite_quota: event.target.checked ? null : 5 })} type="checkbox" />Unlimited</label><Button disabled={disabled} onClick={onCollaborationSave} size="sm">{busy === `${employee.employee_id}:collaboration` ? "Saving…" : "Save policy"}</Button></div>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-[#ebecef] pt-3">
        <div className="flex items-center gap-2 text-xs"><Link2 className="h-3.5 w-3.5 text-[#777c84]" /><span className="font-medium">Feishu / Lark</span>{binding ? <><StatusPill status={binding.lifecycle_status} /><span className="text-[#969aa1]">{binding.runtime_state}</span></> : <span className="text-[#969aa1]">Not connected</span>}</div>
        <div className="flex flex-wrap gap-1.5">
          {binding ? <><Button disabled={disabled || binding.lifecycle_status === "revoked"} ghost onClick={() => onBindingAction("test")} size="sm">Test</Button><Button disabled={disabled || binding.lifecycle_status === "revoked"} ghost onClick={onBinding} size="sm">Update credentials</Button>{binding.lifecycle_status === "active" ? <Button disabled={disabled} ghost onClick={() => onBindingAction("suspended")} size="sm">Suspend binding</Button> : binding.lifecycle_status === "suspended" ? <Button disabled={disabled} ghost onClick={() => onBindingAction("active")} size="sm">Resume binding</Button> : null}<Button disabled={disabled || binding.lifecycle_status === "revoked"} ghost onClick={() => onBindingAction("revoked")} size="sm">Revoke binding</Button></> : <Button disabled={employee.lifecycle_status === "revoked"} ghost onClick={onBinding} size="sm">Connect</Button>}
        </div>
      </div>
    </article>
  );
}

function EmployeeAvatar({ employee, large = false }: { employee: Pick<Employee, "avatar_url" | "profile">; large?: boolean }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [employee.avatar_url]);
  const label = employee.profile?.name || "E";
  const classes = large ? "h-14 w-14" : "h-10 w-10";
  return employee.avatar_url && !failed
    ? <img alt="" className={cn("shrink-0 rounded-full border border-[#e1e3e7] object-cover", classes)} onError={() => setFailed(true)} src={employee.avatar_url} />
    : <span aria-hidden className={cn("flex shrink-0 items-center justify-center rounded-full border border-[#e1e3e7] bg-[#f3f4f6] text-sm font-semibold", classes)}>{label.trim().charAt(0).toUpperCase() || "E"}</span>;
}

function StatusPill({ status }: { status: EmployeeLifecycleStatus }) {
  return <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-medium", status === "active" ? "bg-[#eaf7ef] text-[#237a48]" : status === "suspended" ? "bg-[#fff4dd] text-[#8a5a00]" : "bg-[#fcebea] text-[#a8322d]")}>{LIFECYCLE_LABELS[status]}</span>;
}
