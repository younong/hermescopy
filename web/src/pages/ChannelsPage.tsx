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
  FeishuEmployeeCollaborationPolicy,
  FeishuEmployeePolicy,
  FeishuLifecycleStatus,
  MessagingPlatform,
  MessagingPlatformEnvVar,
  MessagingPlatformsResponse,
  MessagingPlatformUpdate,
} from "@/lib/api";
import { NameCheckboxPicker } from "@/components/NameCheckboxPicker";
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

function allToolsets(catalog: FeishuEmployeeCatalog | null) {
  return catalog?.toolsets.map((item) => item.name) ?? [];
}

function emptyPolicy(catalog: FeishuEmployeeCatalog | null): FeishuEmployeePolicy {
  return {
    schema_version: 1,
    name: "",
    role: "",
    model_registration_id: catalog?.model_registrations[0]?.id ?? "",
    system_prompt: "",
    toolsets: allToolsets(catalog),
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
  const [employeeAvatarFile, setEmployeeAvatarFile] = useState<File | null>(null);
  const [employeeAvatarPreview, setEmployeeAvatarPreview] = useState<string | null>(null);
  const [employeeAvatarRemoved, setEmployeeAvatarRemoved] = useState(false);
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

  useEffect(() => {
    if (!employeeAvatarFile) return;
    const preview = URL.createObjectURL(employeeAvatarFile);
    setEmployeeAvatarPreview(preview);
    return () => URL.revokeObjectURL(preview);
  }, [employeeAvatarFile]);

  const resetEmployeeAvatar = (preview: string | null = null) => {
    setEmployeeAvatarFile(null);
    setEmployeeAvatarPreview(preview);
    setEmployeeAvatarRemoved(false);
  };

  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [employeeBusy, setEmployeeBusy] = useState<string | null>(null);
  const [collaborationDrafts, setCollaborationDrafts] = useState<Record<string, FeishuEmployeeCollaborationPolicy>>({});

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
        setCollaborationDrafts(Object.fromEntries(
          employeeResponse.employees.map((employee) => [employee.account_id, employee.collaboration_policy]),
        ));
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
    resetEmployeeAvatar();
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
    resetEmployeeAvatar(employee.avatar_url);
    setEmployeeDraft((previous) => ({
      ...previous,
      appId: employee.app_id,
      appSecret: "",
      encryptKey: "",
      verificationToken: "",
      policy: employee.profile
        ? { ...employee.profile, toolsets: allToolsets(catalog) }
        : emptyPolicy(catalog),
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
      let savedEmployee: FeishuEmployee | null = null;
      if (employeeEditor.mode === "create") {
        if (!employeeDraft.appId.trim() || !employeeDraft.appSecret) {
          throw new Error("App ID and App Secret are required");
        }
        savedEmployee = await api.createFeishuEmployee({
          app_id: employeeDraft.appId.trim(),
          app_secret: employeeDraft.appSecret,
          domain: employeeDraft.domain,
          encrypt_key: employeeDraft.encryptKey || undefined,
          verification_token: employeeDraft.verificationToken || undefined,
          profile: policy,
          activate: true,
        });
      } else if (employeeEditor.mode === "profile") {
        savedEmployee = await api.updateFeishuEmployeeProfile(employeeEditor.employee.account_id, {
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
      if (savedEmployee && employeeAvatarFile) {
        try {
          await api.uploadFeishuEmployeeAvatar(savedEmployee.account_id, employeeAvatarFile);
        } catch (e) {
          showToast(`Employee saved, but avatar upload failed: ${e}`, "error");
          closeEmployeeEditor();
          await load();
          return;
        }
      } else if (
        savedEmployee
        && employeeEditor.mode === "profile"
        && employeeAvatarRemoved
        && employeeEditor.employee.avatar_url
      ) {
        try {
          await api.deleteFeishuEmployeeAvatar(savedEmployee.account_id);
        } catch (e) {
          showToast(`Employee saved, but avatar removal failed: ${e}`, "error");
          closeEmployeeEditor();
          await load();
          return;
        }
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

  const saveCollaborationPolicy = async (employee: FeishuEmployee) => {
    const draft = collaborationDrafts[employee.account_id] ?? employee.collaboration_policy;
    setEmployeeBusy(`${employee.account_id}:collaboration`);
    try {
      await api.updateFeishuEmployeeCollaborationPolicy(employee.account_id, draft);
      showToast("Collaboration policy saved", "success");
      await load();
    } catch (e) {
      showToast(`Failed to save collaboration policy: ${e}`, "error");
    } finally {
      setEmployeeBusy(null);
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
              {employeeEditor.mode === "credentials" ? (
                <>
                  <p className="text-xs text-muted-foreground">Secret fields are never refilled. Leaving optional fields blank preserves their current encrypted value.</p>
                  <div className="grid gap-1"><Label>New App Secret</Label><Input type="password" value={employeeDraft.appSecret} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, appSecret: event.target.value }))} /></div>
                  <div className="grid gap-1"><Label>New Encrypt Key (optional)</Label><Input type="password" value={employeeDraft.encryptKey} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, encryptKey: event.target.value }))} /></div>
                  <div className="grid gap-1"><Label>New Verification Token (optional)</Label><Input type="password" value={employeeDraft.verificationToken} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, verificationToken: event.target.value }))} /></div>
                </>
              ) : (
                <>
                  <PolicyEditor catalog={catalog} policy={employeeDraft.policy} onChange={(policy) => setEmployeeDraft((previous) => ({ ...previous, policy }))} avatarPreview={employeeAvatarPreview} onAvatarChange={(file) => { setEmployeeAvatarRemoved(false); setEmployeeAvatarFile(file); }} onAvatarRemove={() => { setEmployeeAvatarFile(null); setEmployeeAvatarPreview(null); setEmployeeAvatarRemoved(true); }} />
                  {employeeEditor.mode === "create" && (
                    <fieldset className="grid gap-3 border border-border bg-background/40 p-4">
                      <legend className="px-1 text-xs font-medium">Feishu / Lark app credentials</legend>
                      <p className="text-xs text-muted-foreground">Create, authorize, subscribe, and publish the app in the Feishu/Lark Developer Console first. Hermes connects the app but does not create or delete it.</p>
                      <div className="grid gap-1"><Label>App ID</Label><Input value={employeeDraft.appId} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, appId: event.target.value }))} /></div>
                      <div className="grid gap-1"><Label>App Secret</Label><Input type="password" value={employeeDraft.appSecret} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, appSecret: event.target.value }))} /></div>
                      <div className="grid gap-1"><Label>Domain</Label><select className="h-9 border border-border bg-background px-3 text-sm" value={employeeDraft.domain} onChange={(event) => setEmployeeDraft((previous) => ({ ...previous, domain: event.target.value as "feishu" | "lark" }))}><option value="feishu">Feishu</option><option value="lark">Lark</option></select></div>
                    </fieldset>
                  )}
                </>
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
                    {togglingId === platform.id ? <Spinner /> : <Switch checked={platform.enabled} onCheckedChange={() => void handleToggle(platform)} aria-label={`Enable ${platform.name}`} className="gui-chat-skill-switch" />}
                    {platform.id !== "feishu" && <Button ghost size="sm" onClick={() => handleTest(platform)} disabled={testingId === platform.id} prefix={<PlugZap className="h-4 w-4" />}>Test</Button>}
                    {platform.id !== "feishu" && <Button size="sm" onClick={() => openConfig(platform)} prefix={<Settings2 className="h-4 w-4" />}>Configure</Button>}
                  </div>
                </div>
                {platform.id === "feishu" && (
                  <div className="border-t border-border pt-4 grid gap-3">
                    <div className="flex items-center justify-between gap-3"><div><h3 className="text-sm font-medium">AI employees</h3><p className="text-xs text-muted-foreground">Group messages trigger only on an exact @mention of this bot or a verified direct reply to it.</p></div><Button size="sm" className="gui-chat-workspace-primary-button" onClick={openCreateEmployee} prefix={<UserRoundPlus className="h-4 w-4" />}>Add employee</Button></div>
                    {employees.length === 0 ? <p className="text-xs text-muted-foreground border border-dashed border-border p-4">No managed Feishu employees yet.</p> : employees.map((employee) => <EmployeeRow key={employee.account_id} employee={employee} busy={employeeBusy} collaborationPolicy={collaborationDrafts[employee.account_id] ?? employee.collaboration_policy} onCollaborationPolicyChange={(policy) => setCollaborationDrafts((current) => ({ ...current, [employee.account_id]: policy }))} onCollaborationPolicySave={() => void saveCollaborationPolicy(employee)} onProfile={() => openManagedEmployeeEditor("profile", employee)} onCredentials={() => openManagedEmployeeEditor("credentials", employee)} onAction={(action) => runEmployeeAction(employee, action)} />)}
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

function PolicyEditor({ catalog, policy, onChange, avatarPreview, onAvatarChange, onAvatarRemove }: { catalog: FeishuEmployeeCatalog | null; policy: FeishuEmployeePolicy; onChange: (policy: FeishuEmployeePolicy) => void; avatarPreview: string | null; onAvatarChange: (file: File) => void; onAvatarRemove: () => void }) {
  const fallback = (policy.name || "E").trim().charAt(0).toUpperCase() || "E";
  return (
    <div className="grid gap-4">
      <div className="grid gap-1"><Label>Avatar</Label><div className="flex items-center gap-3"><AvatarImage src={avatarPreview} fallback={fallback} className="h-14 w-14" /><div className="flex flex-wrap gap-2"><label className="inline-flex h-8 cursor-pointer items-center border border-border px-3 text-xs hover:bg-muted/40"><input type="file" accept="image/png,image/jpeg,image/webp" className="hidden" onChange={(event) => { const file = event.currentTarget.files?.[0]; if (file) onAvatarChange(file); event.currentTarget.value = ""; }} />Choose image</label>{avatarPreview && <Button ghost size="sm" onClick={onAvatarRemove}>Remove</Button>}</div></div><p className="text-xs text-muted-foreground">PNG, JPEG, or WebP up to 5 MB.</p></div>
      <div className="grid gap-1"><Label>Name</Label><Input value={policy.name ?? ""} onChange={(event) => onChange({ ...policy, name: event.target.value })} /></div>
      <div className="grid gap-1"><Label>Role</Label><Input value={policy.role ?? ""} onChange={(event) => onChange({ ...policy, role: event.target.value })} /></div>
      <div className="grid gap-1"><Label>Model registration</Label><select className="h-9 border border-border bg-background px-3 text-sm" value={policy.model_registration_id} onChange={(event) => onChange({ ...policy, model_registration_id: event.target.value })}><option value="">Select a model</option>{catalog?.model_registrations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
      <div className="grid gap-1"><Label>System prompt</Label><textarea className="min-h-32 border border-border bg-background p-3 text-sm" value={policy.system_prompt} onChange={(event) => onChange({ ...policy, system_prompt: event.target.value })} /></div>
      <div className="grid gap-1"><Label htmlFor="employee-skills">Skills</Label><NameCheckboxPicker id="employee-skills" available={catalog?.skills ?? []} selected={policy.skills} onChange={(skills) => onChange({ ...policy, skills })} emptyLabel="No skills available." /></div>
      <div className="grid gap-1"><Label>Max iterations</Label><Input type="number" min={1} value={policy.max_iterations} onChange={(event) => onChange({ ...policy, max_iterations: Number(event.target.value) || 1 })} /></div>
    </div>
  );
}

function AvatarImage({ src, fallback, className }: { src: string | null; fallback: string; className: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  return src && !failed
    ? <img src={src} alt="" className={cn("shrink-0 rounded-full border border-border object-cover", className)} onError={() => setFailed(true)} />
    : <span className={cn("flex shrink-0 items-center justify-center rounded-full border border-border bg-muted font-mondwest text-sm", className)} aria-hidden="true">{fallback}</span>;
}

function EmployeeRow({ employee, busy, collaborationPolicy, onCollaborationPolicyChange, onCollaborationPolicySave, onProfile, onCredentials, onAction }: { employee: FeishuEmployee; busy: string | null; collaborationPolicy: FeishuEmployeeCollaborationPolicy; onCollaborationPolicyChange: (policy: FeishuEmployeeCollaborationPolicy) => void; onCollaborationPolicySave: () => void; onProfile: () => void; onCredentials: () => void; onAction: (action: string) => void }) {
  const lifecycle = stateBadge(employee.lifecycle_status);
  const runtime = stateBadge(employee.runtime_state);
  const disabled = busy?.startsWith(`${employee.account_id}:`) ?? false;
  const label = employee.profile?.name || employee.app_id;
  const fallback = label.trim().charAt(0).toUpperCase() || "E";
  const unlimited = collaborationPolicy.invite_quota === null;
  return (
    <div className="grid gap-3 border border-border p-3">
      <div className="flex flex-wrap items-start justify-between gap-3"><div className="flex min-w-0 items-start gap-3"><AvatarImage src={employee.avatar_url} fallback={fallback} className="h-10 w-10" /><div><div className="flex flex-wrap items-center gap-2"><span className="text-sm font-medium">{label}</span><Badge tone={lifecycle.tone}>{lifecycle.label}</Badge><Badge tone={runtime.tone}>{runtime.label}</Badge></div><p className="text-xs text-muted-foreground">{employee.profile?.role || "AI employee"} · profile r{employee.profile_revision ?? "—"} · credentials v{employee.credential_version}</p></div></div><div className="flex flex-wrap gap-2"><Button ghost size="sm" disabled={disabled || employee.lifecycle_status === "revoked"} onClick={() => onAction("test")}>Test</Button><Button ghost size="sm" disabled={disabled || employee.lifecycle_status === "revoked"} onClick={onProfile}>Edit policy</Button><Button ghost size="sm" disabled={disabled || employee.lifecycle_status === "revoked"} onClick={onCredentials}>Rotate secret</Button><Button ghost size="sm" disabled={disabled || employee.lifecycle_status !== "active"} onClick={() => onAction("rollover")}>Roll over sessions</Button>{employee.lifecycle_status === "active" ? <Button ghost size="sm" disabled={disabled} onClick={() => onAction("suspended")}>Suspend</Button> : employee.lifecycle_status === "suspended" ? <Button ghost size="sm" disabled={disabled} onClick={() => onAction("active")}>Resume</Button> : null}<Button ghost size="sm" disabled={disabled || employee.lifecycle_status === "revoked"} onClick={() => onAction("revoked")}>Revoke</Button></div></div>
      {employee.profile?.system_prompt && <p className="text-xs text-muted-foreground line-clamp-2"><Info className="mr-1 inline h-3 w-3" />{employee.profile.system_prompt}</p>}
      <div className="grid gap-3 rounded border border-border bg-background/50 p-3 sm:grid-cols-2">
        <label className="flex items-center justify-between gap-3 text-xs"><span><span className="block font-medium">May participate</span><span className="text-muted-foreground">Can be added to internal groups</span></span><Switch checked={collaborationPolicy.may_participate} onCheckedChange={(checked) => onCollaborationPolicyChange({ ...collaborationPolicy, may_participate: checked })} /></label>
        <label className="flex items-center justify-between gap-3 text-xs"><span><span className="block font-medium">May create groups</span><span className="text-muted-foreground">Employee-initiated group permission</span></span><Switch checked={collaborationPolicy.may_create_groups} onCheckedChange={(checked) => onCollaborationPolicyChange({ ...collaborationPolicy, may_create_groups: checked })} /></label>
        <div className="grid gap-1"><Label>Invite quota</Label><Input aria-label={`Invite quota for ${employee.profile?.name || employee.app_id}`} disabled={unlimited} min={0} type="number" value={collaborationPolicy.invite_quota ?? ""} onChange={(event) => onCollaborationPolicyChange({ ...collaborationPolicy, invite_quota: Math.max(0, Number(event.target.value) || 0) })} /></div>
        <div className="flex items-end justify-between gap-3"><label className="flex items-center gap-2 pb-2 text-xs"><input checked={unlimited} onChange={(event) => onCollaborationPolicyChange({ ...collaborationPolicy, invite_quota: event.target.checked ? null : 5 })} type="checkbox" />Unlimited</label><Button size="sm" disabled={disabled} onClick={onCollaborationPolicySave}>{busy === `${employee.account_id}:collaboration` ? "Saving…" : "Save collaboration"}</Button></div>
      </div>
    </div>
  );
}
