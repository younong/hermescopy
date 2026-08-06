import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  PlugZap,
  Radio,
  Settings2,
  UserRoundPlus,
  X,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { api } from "@/lib/api";
import type {
  FeishuEmployee,
  FeishuEmployeeCatalog,
  FeishuEmployeePolicy,
  FeishuLifecycleStatus,
  MessagingPlatform,
  MessagingPlatformEnvVar,
  MessagingPlatformsResponse,
  MessagingPlatformUpdate,
} from "@/lib/api";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { useDashboardAuthIdentity } from "@/lib/useDashboardAuthIdentity";
import { cn, themedBody } from "@/lib/utils";

const MANAGED_FEISHU_PLATFORM: MessagingPlatform = {
  id: "feishu",
  name: "Feishu / Lark",
  description: "Managed Feishu and Lark AI employees",
  docs_url: "",
  enabled: true,
  configured: true,
  gateway_running: true,
  state: "managed",
  error_code: null,
  error_message: null,
  updated_at: null,
  home_channel: null,
  env_vars: [],
};

const STATE_BADGE: Record<
  string,
  { tone: "success" | "warning" | "destructive" | "secondary" | "outline"; label: string }
> = {
  ready: { tone: "success", label: "Connected" },
  connected: { tone: "success", label: "Connected" },
  active: { tone: "success", label: "Active" },
  suspended: { tone: "warning", label: "Suspended" },
  revoked: { tone: "destructive", label: "Revoked" },
  stopped: { tone: "secondary", label: "Stopped" },
  pending_restart: { tone: "warning", label: "Restart to apply" },
  gateway_stopped: { tone: "warning", label: "Gateway stopped" },
  startup_failed: { tone: "destructive", label: "Start failed" },
  disconnected: { tone: "warning", label: "Disconnected" },
  not_configured: { tone: "outline", label: "Not configured" },
  disabled: { tone: "secondary", label: "Disabled" },
  fatal: { tone: "destructive", label: "Error" },
};

function stateBadge(state: string) {
  return STATE_BADGE[state] ?? { tone: "outline" as const, label: state };
}

function emptyPolicy(catalog: FeishuEmployeeCatalog | null): FeishuEmployeePolicy {
  return {
    schema_version: 1,
    name: "",
    role: "",
    model_registration_id: catalog?.model_registrations[0]?.id ?? "",
    system_prompt: "",
    toolsets: [],
    skills: [],
    mcp_servers: [],
    workspace_relative_path: "employees/new-employee",
    knowledge_relative_paths: [],
    max_iterations: 20,
  };
}

function validateMessagingEnvField(_field: MessagingPlatformEnvVar, _value: string): string | null {
  return null;
}

type EmployeeEditor =
  | { mode: "create" }
  | { mode: "profile" | "credentials"; employee: FeishuEmployee };

export default function ChannelsPage() {
  const { authRequired } = useDashboardAuthIdentity();
  const [platforms, setPlatforms] = useState<MessagingPlatform[]>([]);
  const [envPath, setEnvPath] = useState("~/.hermes/.env");
  const [employees, setEmployees] = useState<FeishuEmployee[]>([]);
  const [catalog, setCatalog] = useState<FeishuEmployeeCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const { toast, showToast } = useToast();

  const [editing, setEditing] = useState<MessagingPlatform | null>(null);
  const [draftEnv, setDraftEnv] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const closeEdit = useCallback(() => {
    setEditing(null);
  }, []);
  const editModalRef = useModalBehavior({ open: editing !== null, onClose: closeEdit });

  const [employeeEditor, setEmployeeEditor] = useState<EmployeeEditor | null>(null);
  const [employeeDraft, setEmployeeDraft] = useState({
    appId: "",
    appSecret: "",
    domain: "feishu" as "feishu" | "lark",
    encryptKey: "",
    verificationToken: "",
    policy: emptyPolicy(null),
  });
  const closeEmployeeEditor = useCallback(() => setEmployeeEditor(null), []);
  const employeeModalRef = useModalBehavior({
    open: employeeEditor !== null,
    onClose: closeEmployeeEditor,
  });

  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [employeeBusy, setEmployeeBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    const platformsRequest = authRequired
      ? Promise.resolve<MessagingPlatformsResponse>({
          env_path: "",
          gateway_start_command: "",
          platforms: [],
        })
      : api.getMessagingPlatforms();
    return Promise.all([
      platformsRequest,
      api.getFeishuEmployees().catch(() => ({ employees: [] })),
      api.getFeishuEmployeeCatalog().catch(() => null),
    ])
      .then(([platformResponse, employeeResponse, catalogResponse]) => {
        setPlatforms(platformResponse.platforms);
        setEnvPath(platformResponse.env_path || "~/.hermes/.env");
        setEmployees(employeeResponse.employees);
        setCatalog(catalogResponse);
      })
      .catch((e) => showToast(`Error: ${e}`, "error"));
  }, [authRequired, showToast]);

  useEffect(() => {
    load().finally(() => setLoading(false));
  }, [load]);

  const openConfig = (platform: MessagingPlatform) => {
    const initial: Record<string, string> = {};
    platform.env_vars.forEach((v) => {
      initial[v.key] = "";
    });
    setDraftEnv(initial);
    setEditing(platform);
  };

  const openCreateEmployee = () => {
    setEmployeeDraft({
      appId: "",
      appSecret: "",
      domain: "feishu",
      encryptKey: "",
      verificationToken: "",
      policy: emptyPolicy(catalog),
    });
    setEmployeeEditor({ mode: "create" });
  };

  const openManagedEmployeeEditor = (
    mode: "profile" | "credentials",
    employee: FeishuEmployee,
  ) => {
    setEmployeeDraft((previous) => ({
      ...previous,
      appId: employee.app_id,
      appSecret: "",
      encryptKey: "",
      verificationToken: "",
      policy: employee.profile ?? emptyPolicy(catalog),
    }));
    setEmployeeEditor({ mode, employee });
  };

  const handleSave = async () => {
    if (!editing) return;
    const env: Record<string, string> = {};
    Object.entries(draftEnv).forEach(([key, value]) => {
      if (value.trim()) env[key] = value.trim();
    });
    if (Object.keys(env).length === 0) {
      showToast("Nothing to save — fill in at least one field.", "error");
      return;
    }
    const missing = editing.env_vars.filter((item) => item.required && !item.is_set && !env[item.key]);
    if (missing.length > 0) {
      showToast(`${missing[0].prompt || missing[0].key} is required`, "error");
      return;
    }
    const nextFieldErrors: Record<string, string> = {};
    editing.env_vars.forEach((field) => {
      const message = validateMessagingEnvField(field, draftEnv[field.key] || "");
      if (message) nextFieldErrors[field.key] = message;
    });
    if (Object.keys(nextFieldErrors).length > 0) {
      showToast("Fix the highlighted fields before saving.", "error");
      return;
    }
    setSaving(true);
    try {
      const body: MessagingPlatformUpdate = { env, enabled: true };
      await api.updateMessagingPlatform(editing.id, body);
      showToast(`${editing.name} saved`, "success");
      closeEdit();
      await load();
    } catch (e) {
      showToast(`Failed to save: ${e}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const handleEmployeeSave = async () => {
    if (!employeeEditor) return;
    const policy = employeeDraft.policy;
    if (!policy.model_registration_id || !policy.system_prompt.trim()) {
      showToast("Select a model and enter a system prompt.", "error");
      return;
    }
    setSaving(true);
    try {
      if (employeeEditor.mode === "create") {
        if (!employeeDraft.appId.trim() || !employeeDraft.appSecret) {
          throw new Error("App ID and App Secret are required");
        }
        await api.createFeishuEmployee({
          app_id: employeeDraft.appId.trim(),
          app_secret: employeeDraft.appSecret,
          domain: employeeDraft.domain,
          encrypt_key: employeeDraft.encryptKey || undefined,
          verification_token: employeeDraft.verificationToken || undefined,
          profile: policy,
          activate: true,
        });
      } else if (employeeEditor.mode === "profile") {
        await api.updateFeishuEmployeeProfile(employeeEditor.employee.account_id, {
          expected_revision: employeeEditor.employee.profile_revision ?? 0,
          profile: policy,
        });
      } else {
        if (!employeeDraft.appSecret) throw new Error("Enter the new App Secret");
        await api.rotateFeishuEmployeeCredentials(employeeEditor.employee.account_id, {
          expected_credential_version: employeeEditor.employee.credential_version,
          app_secret: employeeDraft.appSecret,
          ...(employeeDraft.encryptKey ? { encrypt_key: employeeDraft.encryptKey } : {}),
          ...(employeeDraft.verificationToken
            ? { verification_token: employeeDraft.verificationToken }
            : {}),
        });
      }
      showToast("Feishu AI employee saved", "success");
      closeEmployeeEditor();
      await load();
    } catch (e) {
      showToast(`Failed to save: ${e}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const handleToggle = async (platform: MessagingPlatform) => {
    const next = !platform.enabled;
    setTogglingId(platform.id);
    try {
      await api.updateMessagingPlatform(platform.id, { enabled: next });
      await load();
    } catch (e) {
      showToast(`Error: ${e}`, "error");
    } finally {
      setTogglingId(null);
    }
  };

  const handleTest = async (platform: MessagingPlatform) => {
    setTestingId(platform.id);
    try {
      const res = await api.testMessagingPlatform(platform.id);
      showToast(`${platform.name}: ${res.message}`, res.ok ? "success" : "error");
    } catch (e) {
      showToast(`Error: ${e}`, "error");
    } finally {
      setTestingId(null);
    }
  };

  const runEmployeeAction = async (employee: FeishuEmployee, action: string) => {
    setEmployeeBusy(`${employee.account_id}:${action}`);
    try {
      if (action === "test") {
        const result = await api.testFeishuEmployee(employee.account_id);
        showToast(
          result.ok ? `Connected${result.bot_name ? ` as ${result.bot_name}` : ""}` : "Connection test failed",
          result.ok ? "success" : "error",
        );
      } else if (action === "rollover") {
        const result = await api.rolloverFeishuEmployeeSessions(employee.account_id);
        showToast(`${result.retired_sessions} conversation session(s) rolled over`, "success");
      } else {
        await api.updateFeishuEmployeeLifecycle(
          employee.account_id,
          action as FeishuLifecycleStatus,
        );
        showToast(`Employee ${action}`, "success");
      }
      await load();
    } catch (e) {
      showToast(`Action failed: ${e}`, "error");
    } finally {
      setEmployeeBusy(null);
    }
  };

  const displayedPlatforms = useMemo(
    () => authRequired ? [MANAGED_FEISHU_PLATFORM] : platforms,
    [authRequired, platforms],
  );
  const configured = useMemo(
    () => displayedPlatforms.filter((item) => item.configured).length,
    [displayedPlatforms],
  );

  if (loading) {
    return <div className="flex items-center justify-center py-24"><Spinner className="text-2xl text-primary" /></div>;
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />
      <p className="text-xs text-muted-foreground">
        {authRequired ? (
          <>Managed Feishu App Secrets are encrypted in the control plane.</>
        ) : (
          <>{configured} of {displayedPlatforms.length} channels configured. Legacy credentials are written to{" "}<code className="font-courier">{envPath}</code>.</>
        )}
      </p>

      {editing && (
        <div ref={editModalRef} className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4" role="dialog" aria-modal="true">
          <div className={cn(themedBody, "relative w-full max-w-lg border border-border bg-card shadow-2xl flex flex-col max-h-[90vh]")}>
            <Button ghost size="icon" onClick={closeEdit} className="absolute right-2 top-2" aria-label="Close"><X /></Button>
            <header className="p-5 pb-3 border-b border-border"><h2 className="font-mondwest text-display text-base">Configure {editing.name}</h2></header>
            <div className="p-5 grid gap-4 overflow-y-auto">
              {editing.env_vars.map((field) => (
                <div className="grid gap-1.5" key={field.key}>
                  <Label htmlFor={`field-${field.key}`}>{field.prompt || field.key}{field.required ? " *" : ""}</Label>
                  <Input id={`field-${field.key}`} type={field.is_password ? "password" : "text"} placeholder={field.is_set ? field.redacted_value || "Set — leave blank to keep" : field.key} value={draftEnv[field.key] ?? ""} onChange={(event) => setDraftEnv((previous) => ({ ...previous, [field.key]: event.target.value }))} />
                </div>
              ))}
              <div className="flex justify-end gap-2"><Button ghost size="sm" onClick={closeEdit}>Cancel</Button><Button size="sm" onClick={handleSave} disabled={saving}>{saving ? "Saving…" : "Save & enable"}</Button></div>
            </div>
          </div>
        </div>
      )}

      {employeeEditor && (
        <div ref={employeeModalRef} className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4" role="dialog" aria-modal="true">
          <div className={cn(themedBody, "relative w-full max-w-2xl border border-border bg-card shadow-2xl flex flex-col max-h-[92vh]")}>
            <Button ghost size="icon" onClick={closeEmployeeEditor} className="absolute right-2 top-2" aria-label="Close"><X /></Button>
            <header className="p-5 pb-3 border-b border-border">
              <h2 className="font-mondwest text-display text-base">
                {employeeEditor.mode === "create" ? "Create Feishu AI employee" : employeeEditor.mode === "profile" ? "Edit employee policy" : "Rotate credentials"}
              </h2>
              <p className="mt-1 text-xs text-muted-foreground">Existing conversations keep their immutable policy snapshot. Use session rollover after saving to apply the new profile to subsequent messages.</p>
            </header>
            <div className="p-5 grid gap-4 overflow-y-auto">
              {employeeEditor.mode === "create" && (
                <>
                  <p className="text-xs text-muted-foreground">Create, authorize, subscribe, and publish the app in the Feishu/Lark Developer Console first. Hermes connects the app but does not create or delete it.</p>
                  <div className="grid gap-1"><Label>App ID</Label><Input value={employeeDraft.appId} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, appId: event.target.value }))} /></div>
                  <div className="grid gap-1"><Label>App Secret</Label><Input type="password" value={employeeDraft.appSecret} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, appSecret: event.target.value }))} /></div>
                  <div className="grid gap-1"><Label>Domain</Label><select className="h-9 border border-border bg-background px-3 text-sm" value={employeeDraft.domain} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, domain: event.target.value as "feishu" | "lark" }))}><option value="feishu">Feishu</option><option value="lark">Lark</option></select></div>
                </>
              )}
              {employeeEditor.mode === "credentials" ? (
                <>
                  <p className="text-xs text-muted-foreground">Secret fields are never refilled. Leaving optional fields blank preserves their current encrypted value.</p>
                  <div className="grid gap-1"><Label>New App Secret</Label><Input type="password" value={employeeDraft.appSecret} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, appSecret: event.target.value }))} /></div>
                  <div className="grid gap-1"><Label>New Encrypt Key (optional)</Label><Input type="password" value={employeeDraft.encryptKey} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, encryptKey: event.target.value }))} /></div>
                  <div className="grid gap-1"><Label>New Verification Token (optional)</Label><Input type="password" value={employeeDraft.verificationToken} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, verificationToken: event.target.value }))} /></div>
                </>
              ) : (
                <PolicyEditor catalog={catalog} policy={employeeDraft.policy} onChange={(policy) => setEmployeeDraft((previous) => ({ ...previous, policy }))} />
              )}
              <div className="flex justify-end gap-2"><Button ghost size="sm" onClick={closeEmployeeEditor}>Cancel</Button><Button size="sm" onClick={handleEmployeeSave} disabled={saving}>{saving ? "Saving…" : "Save"}</Button></div>
            </div>
          </div>
        </div>
      )}

      <div className="grid gap-3">
        {displayedPlatforms.map((platform) => {
          const badge = stateBadge(platform.state);
          const StateIcon = platform.state === "connected" ? CheckCircle2 : platform.state === "fatal" || platform.state === "startup_failed" ? AlertTriangle : Radio;
          return (
            <Card key={platform.id} className="border-border">
              <CardContent className="flex flex-col gap-4 p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex items-start gap-3 min-w-0"><StateIcon className="h-5 w-5 shrink-0 mt-0.5 text-muted-foreground" /><div><div className="flex items-center gap-2"><span className="font-mondwest normal-case text-sm font-medium">{platform.name}</span><Badge tone={badge.tone}>{badge.label}</Badge></div><span className="text-xs text-muted-foreground">{platform.description}</span></div></div>
                  <div className="flex items-center gap-2">
                    {togglingId === platform.id ? <Spinner /> : <Switch checked={platform.enabled} onCheckedChange={() => void handleToggle(platform)} aria-label={`Enable ${platform.name}`} />}
                    {platform.id !== "feishu" && <Button ghost size="sm" onClick={() => handleTest(platform)} disabled={testingId === platform.id} prefix={<PlugZap className="h-4 w-4" />}>Test</Button>}
                    {platform.id !== "feishu" && <Button size="sm" onClick={() => openConfig(platform)} prefix={<Settings2 className="h-4 w-4" />}>Configure</Button>}
                  </div>
                </div>
                {platform.id === "feishu" && (
                  <div className="border-t border-border pt-4 grid gap-3">
                    <div className="flex items-center justify-between gap-3"><div><h3 className="text-sm font-medium">AI employees</h3><p className="text-xs text-muted-foreground">Group messages trigger only on an exact @mention of this bot or a verified direct reply to it.</p></div><Button size="sm" onClick={openCreateEmployee} prefix={<UserRoundPlus className="h-4 w-4" />}>Add employee</Button></div>
                    {employees.length === 0 ? <p className="text-xs text-muted-foreground border border-dashed border-border p-4">No managed Feishu employees yet.</p> : employees.map((employee) => <EmployeeRow key={employee.account_id} employee={employee} busy={employeeBusy} onProfile={() => openManagedEmployeeEditor("profile", employee)} onCredentials={() => openManagedEmployeeEditor("credentials", employee)} onAction={(action) => runEmployeeAction(employee, action)} />)}
                  </div>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

function PolicyEditor({ catalog, policy, onChange }: { catalog: FeishuEmployeeCatalog | null; policy: FeishuEmployeePolicy; onChange: (policy: FeishuEmployeePolicy) => void }) {
  const updateList = (field: "toolsets" | "skills" | "mcp_servers", value: string) => onChange({ ...policy, [field]: value.split(",").map((item) => item.trim()).filter(Boolean) });
  return (
    <div className="grid gap-4">
      <div className="grid gap-1"><Label>Name</Label><Input value={policy.name ?? ""} onChange={(event) => onChange({ ...policy, name: event.target.value })} /></div>
      <div className="grid gap-1"><Label>Role</Label><Input value={policy.role ?? ""} onChange={(event) => onChange({ ...policy, role: event.target.value })} /></div>
      <div className="grid gap-1"><Label>Model registration</Label><select className="h-9 border border-border bg-background px-3 text-sm" value={policy.model_registration_id} onChange={(event) => onChange({ ...policy, model_registration_id: event.target.value })}><option value="">Select a model</option>{catalog?.model_registrations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
      <div className="grid gap-1"><Label>System prompt</Label><textarea className="min-h-32 border border-border bg-background p-3 text-sm" value={policy.system_prompt} onChange={(event) => onChange({ ...policy, system_prompt: event.target.value })} /></div>
      <div className="grid gap-1"><Label>Toolsets</Label><Input placeholder={catalog?.toolsets.map((item) => item.name).join(", ")} value={policy.toolsets.join(", ")} onChange={(event) => updateList("toolsets", event.target.value)} /></div>
      <div className="grid gap-1"><Label>Skills</Label><Input placeholder={catalog?.skills.map((item) => item.name).join(", ")} value={policy.skills.join(", ")} onChange={(event) => updateList("skills", event.target.value)} /></div>
      <div className="grid gap-1"><Label>MCP servers</Label><Input placeholder={catalog?.mcp_servers.join(", ")} value={policy.mcp_servers.join(", ")} onChange={(event) => updateList("mcp_servers", event.target.value)} /></div>
      <div className="grid gap-1"><Label>Workspace relative path</Label><Input value={policy.workspace_relative_path} onChange={(event) => onChange({ ...policy, workspace_relative_path: event.target.value })} /></div>
      <div className="grid gap-1"><Label>Knowledge relative paths</Label><Input value={policy.knowledge_relative_paths.join(", ")} onChange={(event) => onChange({ ...policy, knowledge_relative_paths: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) })} /></div>
      <div className="grid gap-1"><Label>Max iterations</Label><Input type="number" min={1} value={policy.max_iterations} onChange={(event) => onChange({ ...policy, max_iterations: Number(event.target.value) || 1 })} /></div>
    </div>
  );
}

function EmployeeRow({ employee, busy, onProfile, onCredentials, onAction }: { employee: FeishuEmployee; busy: string | null; onProfile: () => void; onCredentials: () => void; onAction: (action: string) => void }) {
  const lifecycle = stateBadge(employee.lifecycle_status);
  const runtime = stateBadge(employee.runtime_state);
  const disabled = busy?.startsWith(`${employee.account_id}:`) ?? false;
  return (
    <div className="grid gap-3 border border-border p-3">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex flex-wrap items-center gap-2"><span className="text-sm font-medium">{employee.profile?.name || employee.app_id}</span><Badge tone={lifecycle.tone}>{lifecycle.label}</Badge><Badge tone={runtime.tone}>{runtime.label}</Badge></div><p className="text-xs text-muted-foreground">{employee.profile?.role || "AI employee"} · profile r{employee.profile_revision ?? "—"} · credentials v{employee.credential_version}</p></div><div className="flex flex-wrap gap-2"><Button ghost size="sm" disabled={disabled || employee.lifecycle_status === "revoked"} onClick={() => onAction("test")}>Test</Button><Button ghost size="sm" disabled={disabled || employee.lifecycle_status === "revoked"} onClick={onProfile}>Edit policy</Button><Button ghost size="sm" disabled={disabled || employee.lifecycle_status === "revoked"} onClick={onCredentials}>Rotate secret</Button><Button ghost size="sm" disabled={disabled || employee.lifecycle_status !== "active"} onClick={() => onAction("rollover")}>Roll over sessions</Button>{employee.lifecycle_status === "active" ? <Button ghost size="sm" disabled={disabled} onClick={() => onAction("suspended")}>Suspend</Button> : employee.lifecycle_status === "suspended" ? <Button ghost size="sm" disabled={disabled} onClick={() => onAction("active")}>Resume</Button> : null}<Button ghost size="sm" disabled={disabled || employee.lifecycle_status === "revoked"} onClick={() => onAction("revoked")}>Revoke</Button></div></div>
      {employee.profile?.system_prompt && <p className="text-xs text-muted-foreground line-clamp-2"><Info className="mr-1 inline h-3 w-3" />{employee.profile.system_prompt}</p>}
    </div>
  );
}
