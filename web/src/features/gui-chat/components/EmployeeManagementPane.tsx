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

const LIFECYCLE_LABELS: Record<EmployeeLifecycleStatus, string> = {
  active: "启用",
  revoked: "已撤销",
  suspended: "已暂停",
};

const BINDING_RUNTIME_LABELS: Record<string, string> = {
  connected: "已连接",
  connecting: "连接中",
  error: "异常",
  failed: "失败",
  running: "运行中",
  stopped: "已停止",
};

function bindingRuntimeLabel(runtimeState: string) {
  return BINDING_RUNTIME_LABELS[runtimeState.toLowerCase()] ?? "状态未知";
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

export function EmployeeManagementPane() {
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
  }, []);

  useEffect(() => {
    void Promise.all([refreshEmployees(), api.getEmployeeCatalog().then(setCatalog)])
      .catch((error: unknown) => showToast(`无法加载员工：${String(error)}`, "error"))
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
      showToast("请输入名称、选择模型并填写系统提示词。", "error");
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
      showToast(editor.mode === "create" ? "员工已创建" : "员工资料已保存", "success");
      closeEditor();
      await refreshEmployees();
    } catch (error) {
      showToast(`无法保存员工：${String(error)}`, "error");
    } finally {
      setBusy(null);
    }
  };

  const saveBinding = async () => {
    if (!bindingEditor || !bindingDraft.appSecret) {
      showToast("请输入应用密钥。", "error");
      return;
    }
    setBusy("binding:save");
    try {
      const { employee, mode } = bindingEditor;
      if (mode === "create") {
        if (!bindingDraft.appId.trim()) throw new Error("请输入应用标识");
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
        if (!binding) throw new Error("飞书或 Lark 绑定已不可用");
        await api.updateEmployeeFeishuBinding(employee.employee_id, {
          app_secret: bindingDraft.appSecret,
          encrypt_key: bindingDraft.encryptKey || undefined,
          expected_credential_version: binding.credential_version,
          verification_token: bindingDraft.verificationToken || undefined,
        });
      }
      showToast(mode === "create" ? "飞书 / Lark 已连接" : "绑定凭据已更新", "success");
      closeBindingEditor();
      await refreshEmployees();
    } catch (error) {
      showToast(`无法保存绑定：${String(error)}`, "error");
    } finally {
      setBusy(null);
    }
  };

  const saveCollaboration = async (employee: Employee) => {
    const draft = collaborationDrafts[employee.employee_id] ?? employee.collaboration_policy;
    setBusy(`${employee.employee_id}:collaboration`);
    try {
      await api.updateEmployeeCollaborationPolicy(employee.employee_id, draft);
      showToast("协作权限已保存", "success");
      await refreshEmployees();
    } catch (error) {
      showToast(`无法保存协作权限：${String(error)}`, "error");
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
        showToast(`已更新 ${result.retired_sessions} 个对话会话`, "success");
      } else {
        await api.updateEmployeeLifecycle(employee.employee_id, action);
        showToast(`员工状态已更新为“${LIFECYCLE_LABELS[action]}”`, "success");
      }
      await refreshEmployees();
    } catch (error) {
      showToast(`员工操作失败：${String(error)}`, "error");
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
          result.ok ? `连接成功${result.bot_name ? `：${result.bot_name}` : ""}` : "连接测试失败",
          result.ok ? "success" : "error",
        );
      } else {
        await api.updateEmployeeFeishuBindingLifecycle(employee.employee_id, action);
        showToast(`飞书 / Lark 绑定状态已更新为“${LIFECYCLE_LABELS[action]}”`, "success");
      }
      await refreshEmployees();
    } catch (error) {
      showToast(`绑定操作失败：${String(error)}`, "error");
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
          <h2 className="text-[15px] font-semibold text-[#25282d]">员工</h2>
          <p className="mt-1 text-xs text-[#777c84]">创建专注的 AI 员工，用于直接对话和内部协作。</p>
        </div>
        <Button className="gui-chat-workspace-primary-button" onClick={openCreate} size="sm" prefix={<Plus className="h-4 w-4" />}>添加员工</Button>
      </div>

      {employees.length === 0 ? (
        <div className="rounded-xl border border-dashed border-[#dfe2e7] px-5 py-10 text-center">
          <Bot className="mx-auto h-6 w-6 text-[#969aa1]" />
          <p className="mt-2 text-sm font-medium">暂无员工</p>
          <p className="mt-1 text-xs text-[#969aa1]">无需连接消息渠道即可添加员工。</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-[#e1e3e7] bg-white">
          <div aria-hidden className="hidden grid-cols-[minmax(0,1fr)_7rem_9rem_5rem] gap-4 border-b border-[#e1e3e7] bg-[#f8f9fa] px-4 py-2 text-[11px] font-medium text-[#777c84] sm:grid">
            <span>员工</span><span>状态</span><span>飞书 / Lark</span><span className="text-right">操作</span>
          </div>
          <ul aria-label="员工列表" className="divide-y divide-[#e8eaed]" role="list">
            {employees.map((employee) => (
              <EmployeeListItem employee={employee} key={employee.employee_id} onManage={() => setManagedEmployeeId(employee.employee_id)} />
            ))}
          </ul>
        </div>
      )}

      {managedEmployee ? (
        <div aria-label={`管理员工：${managedEmployee.profile?.name || "未命名员工"}`} aria-modal="true" className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" ref={managementRef} role="dialog">
          <div className="relative flex max-h-[92vh] w-full max-w-2xl flex-col rounded-xl border border-[#e1e3e7] bg-white shadow-2xl">
            <button aria-label="关闭" className="gui-chat-icon-button absolute right-3 top-3" onClick={closeManagement} type="button"><X /></button>
            <div className="border-b border-[#ebecef] px-5 py-4">
              <h3 className="text-[15px] font-semibold">{managedEmployee.profile?.name || "未命名员工"}</h3>
              <p className="mt-1 text-[11px] text-[#969aa1]">管理员工资料、协作权限、生命周期和消息渠道。</p>
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
              />
            </div>
          </div>
        </div>
      ) : null}

      {editor ? (
        <div aria-label={editor.mode === "create" ? "添加员工" : "编辑员工"} aria-modal="true" className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" ref={editorRef} role="dialog">
          <div className="relative flex max-h-[92vh] w-full max-w-2xl flex-col rounded-xl border border-[#e1e3e7] bg-white shadow-2xl">
            <button aria-label="关闭" className="gui-chat-icon-button absolute right-3 top-3" onClick={closeEditor} type="button"><X /></button>
            <div className="border-b border-[#ebecef] px-5 py-4">
              <h3 className="text-[15px] font-semibold">{editor.mode === "create" ? "添加员工" : "编辑员工"}</h3>
              <p className="mt-1 text-[11px] text-[#969aa1]">资料更新仅应用于新会话。如需让现有会话使用最新版本，请更新会话。</p>
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
                <Button ghost onClick={closeEditor} size="sm">取消</Button>
                <Button disabled={busy === "employee:save"} onClick={() => void saveEmployee()} size="sm">{busy === "employee:save" ? "保存中…" : "保存"}</Button>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {bindingEditor ? (
        <div aria-label="飞书 / Lark 绑定" aria-modal="true" className="fixed inset-0 z-[100] flex items-center justify-center bg-black/35 p-4" ref={bindingEditorRef} role="dialog">
          <div className="relative w-full max-w-lg rounded-xl border border-[#e1e3e7] bg-white p-5 shadow-2xl">
            <button aria-label="关闭" className="gui-chat-icon-button absolute right-3 top-3" onClick={closeBindingEditor} type="button"><X /></button>
            <h3 className="text-[15px] font-semibold">{bindingEditor.mode === "create" ? "连接飞书 / Lark" : "更新飞书 / Lark 凭据"}</h3>
            <p className="mt-1 text-[11px] text-[#969aa1]">此项可选。每位员工最多绑定一个飞书或 Lark 应用。</p>
            <div className="mt-4 grid gap-3">
              {bindingEditor.mode === "create" ? (
                <>
                  <Field label="应用标识"><Input value={bindingDraft.appId} onChange={(event) => setBindingDraft((current) => ({ ...current, appId: event.target.value }))} /></Field>
                  <Field label="平台"><select className="h-9 rounded-md border border-[#dfe2e7] bg-white px-3 text-sm" value={bindingDraft.domain} onChange={(event) => setBindingDraft((current) => ({ ...current, domain: event.target.value as "feishu" | "lark" }))}><option value="feishu">飞书</option><option value="lark">Lark</option></select></Field>
                </>
              ) : null}
              <Field label={bindingEditor.mode === "create" ? "应用密钥" : "新应用密钥"}><Input type="password" value={bindingDraft.appSecret} onChange={(event) => setBindingDraft((current) => ({ ...current, appSecret: event.target.value }))} /></Field>
              <Field label="加密密钥（可选）"><Input type="password" value={bindingDraft.encryptKey} onChange={(event) => setBindingDraft((current) => ({ ...current, encryptKey: event.target.value }))} /></Field>
              <Field label="验证令牌（可选）"><Input type="password" value={bindingDraft.verificationToken} onChange={(event) => setBindingDraft((current) => ({ ...current, verificationToken: event.target.value }))} /></Field>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <Button ghost onClick={closeBindingEditor} size="sm">取消</Button>
              <Button disabled={busy === "binding:save"} onClick={() => void saveBinding()} size="sm">{busy === "binding:save" ? "保存中…" : "保存"}</Button>
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
      <Field label="头像">
        <div className="flex items-center gap-3">
          <EmployeeAvatar employee={{ avatar_url: avatarPreview, profile: policy }} large />
          <div className="flex gap-2">
            <label className="inline-flex h-8 cursor-pointer items-center rounded-md border border-[#dfe2e7] px-3 text-xs hover:bg-[#f6f7f9]"><input accept="image/jpeg,image/png,image/webp" className="hidden" onChange={(event) => { const file = event.currentTarget.files?.[0]; if (file) onAvatarChange(file); event.currentTarget.value = ""; }} type="file" />选择图片</label>
            {avatarPreview ? <Button ghost onClick={onAvatarRemove} size="sm">移除</Button> : null}
          </div>
        </div>
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="名称"><Input value={policy.name ?? ""} onChange={(event) => onChange({ ...policy, name: event.target.value })} /></Field>
        <Field label="角色"><Input value={policy.role ?? ""} onChange={(event) => onChange({ ...policy, role: event.target.value })} /></Field>
      </div>
      <Field label="模型"><select className="h-9 rounded-md border border-[#dfe2e7] bg-white px-3 text-sm" value={policy.model_registration_id} onChange={(event) => onChange({ ...policy, model_registration_id: event.target.value })}><option value="">选择模型</option>{catalog?.model_registrations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
      <Field label="系统提示词"><textarea className="min-h-28 rounded-md border border-[#dfe2e7] bg-white p-3 text-sm" value={policy.system_prompt} onChange={(event) => onChange({ ...policy, system_prompt: event.target.value })} /></Field>
      <Field label="技能"><NameCheckboxPicker available={catalog?.skills ?? []} emptyLabel="暂无可用技能。" id="employee-skills" onChange={(skills) => onChange({ ...policy, skills })} selected={policy.skills} /></Field>
      <Field label="最大迭代次数"><Input min={1} onChange={(event) => onChange({ ...policy, max_iterations: Number(event.target.value) || 1 })} type="number" value={policy.max_iterations} /></Field>
    </div>
  );
}

function EmployeeListItem({ employee, onManage }: { employee: Employee; onManage(): void }) {
  const binding = employee.channels.feishu;
  return (
    <li className="grid min-h-16 items-center gap-3 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_7rem_9rem_5rem] sm:gap-4" role="listitem">
      <div className="flex min-w-0 items-center gap-3">
        <EmployeeAvatar employee={employee} />
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium">{employee.profile?.name || "未命名员工"}</h3>
          <p className="truncate text-[11px] text-[#969aa1]">{employee.profile?.role || "AI 员工"} · 资料版本 {employee.profile_revision ?? "—"}</p>
        </div>
      </div>
      <div><StatusPill status={employee.lifecycle_status} /></div>
      <div className="flex items-center gap-2 text-xs text-[#777c84]">
        <Link2 className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate">{binding ? bindingRuntimeLabel(binding.runtime_state) : "未连接"}</span>
      </div>
      <div className="sm:text-right"><Button ghost onClick={onManage} size="sm">管理</Button></div>
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
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <EmployeeAvatar employee={employee} />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2"><h3 className="truncate text-sm font-semibold">{employee.profile?.name || "未命名员工"}</h3><StatusPill status={employee.lifecycle_status} /></div>
            <p className="truncate text-[11px] text-[#969aa1]">{employee.profile?.role || "AI 员工"} · 资料版本 {employee.profile_revision ?? "—"}</p>
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Button disabled={disabled || employee.lifecycle_status === "revoked"} ghost onClick={onProfile} size="sm">编辑资料</Button>
          <Button disabled={disabled || employee.lifecycle_status !== "active"} ghost onClick={() => onEmployeeAction("rollover")} size="sm">更新会话</Button>
          {employee.lifecycle_status === "active" ? <Button disabled={disabled} ghost onClick={() => onEmployeeAction("suspended")} size="sm">暂停</Button> : employee.lifecycle_status === "suspended" ? <Button disabled={disabled} ghost onClick={() => onEmployeeAction("active")} size="sm">恢复</Button> : null}
          <Button disabled={disabled || employee.lifecycle_status === "revoked"} ghost onClick={() => onEmployeeAction("revoked")} size="sm">撤销</Button>
        </div>
      </div>

      <div className="mt-4 grid gap-3 border-l-2 border-[#e8eaed] pl-3 sm:grid-cols-2">
        <label className="flex items-center justify-between gap-3 text-xs"><span><span className="block font-medium">允许参与协作</span><span className="text-[#969aa1]">可加入内部群组</span></span><Switch checked={collaborationPolicy.may_participate} className="gui-chat-skill-switch" onCheckedChange={(checked) => onCollaborationChange({ ...collaborationPolicy, may_participate: checked })} /></label>
        <label className="flex items-center justify-between gap-3 text-xs"><span><span className="block font-medium">允许创建群组</span><span className="text-[#969aa1]">可邀请其他员工</span></span><Switch checked={collaborationPolicy.may_create_groups} className="gui-chat-skill-switch" onCheckedChange={(checked) => onCollaborationChange({ ...collaborationPolicy, may_create_groups: checked })} /></label>
        <Field label="邀请名额"><Input aria-label={`${employee.profile?.name || employee.employee_id}的邀请名额`} disabled={unlimited} min={0} onChange={(event) => onCollaborationChange({ ...collaborationPolicy, invite_quota: Math.max(0, Number(event.target.value) || 0) })} type="number" value={collaborationPolicy.invite_quota ?? ""} /></Field>
        <div className="flex items-end justify-between gap-3"><label className="flex items-center gap-2 pb-2 text-xs"><input checked={unlimited} onChange={(event) => onCollaborationChange({ ...collaborationPolicy, invite_quota: event.target.checked ? null : 5 })} type="checkbox" />不限制</label><Button disabled={disabled} onClick={onCollaborationSave} size="sm">{busy === `${employee.employee_id}:collaboration` ? "保存中…" : "保存权限"}</Button></div>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-[#ebecef] pt-3">
        <div className="flex items-center gap-2 text-xs"><Link2 className="h-3.5 w-3.5 text-[#777c84]" /><span className="font-medium">飞书 / Lark</span>{binding ? <><StatusPill status={binding.lifecycle_status} /><span className="text-[#969aa1]">{bindingRuntimeLabel(binding.runtime_state)}</span></> : <span className="text-[#969aa1]">未连接</span>}</div>
        <div className="flex flex-wrap gap-1.5">
          {binding ? <><Button disabled={disabled || binding.lifecycle_status === "revoked"} ghost onClick={() => onBindingAction("test")} size="sm">测试连接</Button><Button disabled={disabled || binding.lifecycle_status === "revoked"} ghost onClick={onBinding} size="sm">更新凭据</Button>{binding.lifecycle_status === "active" ? <Button disabled={disabled} ghost onClick={() => onBindingAction("suspended")} size="sm">暂停绑定</Button> : binding.lifecycle_status === "suspended" ? <Button disabled={disabled} ghost onClick={() => onBindingAction("active")} size="sm">恢复绑定</Button> : null}<Button disabled={disabled || binding.lifecycle_status === "revoked"} ghost onClick={() => onBindingAction("revoked")} size="sm">撤销绑定</Button></> : <Button disabled={employee.lifecycle_status === "revoked"} ghost onClick={onBinding} size="sm">连接</Button>}
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

function StatusPill({ status }: { status: EmployeeLifecycleStatus }) {
  return <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-medium", status === "active" ? "bg-[#eaf7ef] text-[#237a48]" : status === "suspended" ? "bg-[#fff4dd] text-[#8a5a00]" : "bg-[#fcebea] text-[#a8322d]")}>{LIFECYCLE_LABELS[status]}</span>;
}
