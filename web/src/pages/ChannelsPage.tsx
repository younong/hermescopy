import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, PlugZap, Radio, Settings2, X } from "lucide-react";
import { Link } from "react-router-dom";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { api } from "@/lib/api";
import type {
  Employee,
  MessagingPlatform,
  MessagingPlatformEnvVar,
  MessagingPlatformsResponse,
  MessagingPlatformUpdate,
} from "@/lib/api";
import { useDashboardAuthIdentity } from "@/lib/useDashboardAuthIdentity";
import { cn, themedBody } from "@/lib/utils";

const MANAGED_FEISHU_PLATFORM: MessagingPlatform = {
  configured: true,
  description: "Managed Feishu and Lark employee connections",
  docs_url: "",
  enabled: true,
  env_vars: [],
  error_code: null,
  error_message: null,
  gateway_running: true,
  home_channel: null,
  id: "feishu",
  name: "Feishu / Lark",
  state: "managed",
  updated_at: null,
};

const STATE_BADGE: Record<
  string,
  { tone: "success" | "warning" | "destructive" | "secondary" | "outline"; label: string }
> = {
  connected: { label: "Connected", tone: "success" },
  disabled: { label: "Disabled", tone: "secondary" },
  disconnected: { label: "Disconnected", tone: "warning" },
  fatal: { label: "Error", tone: "destructive" },
  gateway_stopped: { label: "Gateway stopped", tone: "warning" },
  managed: { label: "Managed", tone: "success" },
  not_configured: { label: "Not configured", tone: "outline" },
  pending_restart: { label: "Restart to apply", tone: "warning" },
  startup_failed: { label: "Start failed", tone: "destructive" },
  stopped: { label: "Stopped", tone: "secondary" },
};

function stateBadge(state: string) {
  return STATE_BADGE[state] ?? { label: state, tone: "outline" as const };
}

function validateMessagingEnvField(_field: MessagingPlatformEnvVar, _value: string): string | null {
  return null;
}

export default function ChannelsPage() {
  const { authRequired } = useDashboardAuthIdentity();
  const [platforms, setPlatforms] = useState<MessagingPlatform[]>([]);
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [envPath, setEnvPath] = useState("~/.hermes/.env");
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<MessagingPlatform | null>(null);
  const [draftEnv, setDraftEnv] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const { toast, showToast } = useToast();
  const closeEdit = useCallback(() => setEditing(null), []);
  const editModalRef = useModalBehavior({ onClose: closeEdit, open: editing !== null });

  const refreshPlatforms = useCallback(async () => {
    const response = authRequired
      ? {
          env_path: "",
          gateway_start_command: "",
          platforms: [],
        } satisfies MessagingPlatformsResponse
      : await api.getMessagingPlatforms();
    setPlatforms(response.platforms);
    setEnvPath(response.env_path || "~/.hermes/.env");
  }, [authRequired]);

  const load = useCallback(async () => {
    const [, employeeResponse] = await Promise.all([
      refreshPlatforms(),
      api.getEmployees().catch(() => ({ employees: [] })),
    ]);
    setEmployees(employeeResponse.employees);
  }, [refreshPlatforms]);

  useEffect(() => {
    void load()
      .catch((error: unknown) => showToast(`Error: ${String(error)}`, "error"))
      .finally(() => setLoading(false));
  }, [load, showToast]);

  const displayedPlatforms = useMemo(
    () => authRequired ? [MANAGED_FEISHU_PLATFORM] : platforms,
    [authRequired, platforms],
  );
  const configured = useMemo(
    () => displayedPlatforms.filter((platform) => platform.configured).length,
    [displayedPlatforms],
  );
  const feishuBindings = useMemo(
    () => employees.map((employee) => employee.channels.feishu).filter(Boolean),
    [employees],
  );

  const openConfig = (platform: MessagingPlatform) => {
    setDraftEnv(Object.fromEntries(platform.env_vars.map((field) => [field.key, ""])));
    setEditing(platform);
  };

  const handleSave = async () => {
    if (!editing) return;
    const env = Object.fromEntries(
      Object.entries(draftEnv).filter(([, value]) => value.trim()).map(([key, value]) => [key, value.trim()]),
    );
    if (Object.keys(env).length === 0) {
      showToast("Nothing to save — fill in at least one field.", "error");
      return;
    }
    const missing = editing.env_vars.find((field) => field.required && !field.is_set && !env[field.key]);
    if (missing) {
      showToast(`${missing.prompt || missing.key} is required`, "error");
      return;
    }
    const invalid = editing.env_vars.find((field) => validateMessagingEnvField(field, draftEnv[field.key] || ""));
    if (invalid) {
      showToast("Fix the highlighted fields before saving.", "error");
      return;
    }
    setSaving(true);
    try {
      const body: MessagingPlatformUpdate = { enabled: true, env };
      await api.updateMessagingPlatform(editing.id, body);
      showToast(`${editing.name} saved`, "success");
      closeEdit();
      await refreshPlatforms();
    } catch (error) {
      showToast(`Failed to save: ${String(error)}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const handleToggle = async (platform: MessagingPlatform) => {
    setTogglingId(platform.id);
    try {
      await api.updateMessagingPlatform(platform.id, { enabled: !platform.enabled });
      await refreshPlatforms();
    } catch (error) {
      showToast(`Error: ${String(error)}`, "error");
    } finally {
      setTogglingId(null);
    }
  };

  const handleTest = async (platform: MessagingPlatform) => {
    setTestingId(platform.id);
    try {
      const result = await api.testMessagingPlatform(platform.id);
      showToast(`${platform.name}: ${result.message}`, result.ok ? "success" : "error");
    } catch (error) {
      showToast(`Error: ${String(error)}`, "error");
    } finally {
      setTestingId(null);
    }
  };

  if (loading) {
    return <div className="flex items-center justify-center py-24"><Spinner className="text-2xl text-primary" /></div>;
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />
      <p className="text-xs text-muted-foreground">
        {authRequired
          ? "Channel connections are managed per employee."
          : <>{configured} of {displayedPlatforms.length} channels configured. Legacy credentials are written to <code className="font-courier">{envPath}</code>.</>}
      </p>

      {editing ? (
        <div aria-modal="true" className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4" ref={editModalRef} role="dialog">
          <div className={cn(themedBody, "relative flex max-h-[90vh] w-full max-w-lg flex-col border border-border bg-card shadow-2xl")}>
            <Button aria-label="Close" className="absolute right-2 top-2" ghost onClick={closeEdit} size="icon"><X /></Button>
            <header className="border-b border-border p-5 pb-3"><h2 className="font-mondwest text-display text-base">Configure {editing.name}</h2></header>
            <div className="grid gap-4 overflow-y-auto p-5">
              {editing.env_vars.map((field) => (
                <div className="grid gap-1.5" key={field.key}>
                  <Label htmlFor={`field-${field.key}`}>{field.prompt || field.key}{field.required ? " *" : ""}</Label>
                  <Input id={`field-${field.key}`} onChange={(event) => setDraftEnv((current) => ({ ...current, [field.key]: event.target.value }))} placeholder={field.is_set ? field.redacted_value || "Set — leave blank to keep" : field.key} type={field.is_password ? "password" : "text"} value={draftEnv[field.key] ?? ""} />
                </div>
              ))}
              <div className="flex justify-end gap-2"><Button ghost onClick={closeEdit} size="sm">Cancel</Button><Button disabled={saving} onClick={() => void handleSave()} size="sm">{saving ? "Saving…" : "Save & enable"}</Button></div>
            </div>
          </div>
        </div>
      ) : null}

      <div className="grid gap-3">
        {displayedPlatforms.map((platform) => {
          const badge = stateBadge(platform.state);
          const StateIcon = platform.state === "connected" || platform.state === "managed"
            ? CheckCircle2
            : platform.state === "fatal" || platform.state === "startup_failed"
              ? AlertTriangle
              : Radio;
          const isManagedFeishu = authRequired && platform.id === "feishu";
          return (
            <Card className="border-border" key={platform.id}>
              <CardContent className="flex flex-col gap-4 p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex min-w-0 items-start gap-3"><StateIcon className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" /><div><div className="flex items-center gap-2"><span className="font-mondwest text-sm font-medium normal-case">{platform.name}</span><Badge tone={badge.tone}>{badge.label}</Badge></div><span className="text-xs text-muted-foreground">{platform.description}</span></div></div>
                  {isManagedFeishu ? (
                    <Link className="inline-flex h-8 items-center rounded-md bg-midground px-3 text-xs font-medium text-background-base hover:bg-midground/90" to="/chat/robots">Manage employees</Link>
                  ) : (
                    <div className="flex items-center gap-2">
                      {togglingId === platform.id ? <Spinner /> : <Switch aria-label={`Enable ${platform.name}`} checked={platform.enabled} onCheckedChange={() => void handleToggle(platform)} />}
                      <Button disabled={testingId === platform.id} ghost onClick={() => void handleTest(platform)} prefix={<PlugZap className="h-4 w-4" />} size="sm">Test</Button>
                      <Button onClick={() => openConfig(platform)} prefix={<Settings2 className="h-4 w-4" />} size="sm">Configure</Button>
                    </div>
                  )}
                </div>
                {isManagedFeishu ? (
                  <div className="border-t border-border pt-3 text-xs text-muted-foreground">
                    {feishuBindings.length === 0
                      ? "No employee Feishu / Lark bindings are configured."
                      : `${feishuBindings.length} employee binding${feishuBindings.length === 1 ? "" : "s"}: ${feishuBindings.filter((binding) => binding?.lifecycle_status === "active").length} active.`}
                  </div>
                ) : null}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
