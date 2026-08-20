import { React, useEffect, useState } from "../runtime";
import { WandSparkles } from "../runtime";
import { kanbanTranslations, useI18n } from "../runtime";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";
import type { KanbanApi } from "../api";
import { kanbanErrorMessage } from "../errors";
import type { KanbanOrchestrationSettings, KanbanProfile } from "../types";

export function KanbanSettingsDialog({
  api,
  onClose,
}: {
  api: KanbanApi;
  onClose(): void;
}) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  const [profiles, setProfiles] = useState<KanbanProfile[]>([]);
  const [settings, setSettings] = useState<KanbanOrchestrationSettings | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.listProfiles(), api.getOrchestration()])
      .then(([roster, orchestration]) => {
        if (cancelled) return;
        setProfiles(roster.profiles);
        setSettings(orchestration);
      })
      .catch((cause) => !cancelled && setError(kanbanErrorMessage(cause)))
      .finally(() => !cancelled && setBusy(false));
    return () => { cancelled = true; };
  }, [api]);

  const save = async () => {
    if (!settings) return;
    setBusy(true);
    setError(null);
    try {
      setSettings(await api.updateOrchestration({
        auto_decompose: settings.auto_decompose,
        auto_promote_children: settings.auto_promote_children,
        default_assignee: settings.default_assignee,
        orchestrator_profile: settings.orchestrator_profile,
      }));
      onClose();
    } catch (cause) {
      setError(kanbanErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const saveDescription = async (profile: KanbanProfile, description: string) => {
    setBusy(true);
    try {
      const result = await api.updateProfileDescription(profile.name, description);
      setProfiles((current) => current.map((item) => item.name === profile.name ? { ...item, description: result.description, description_auto: false } : item));
    } catch (cause) {
      setError(kanbanErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const autoDescribe = async (profile: KanbanProfile) => {
    setBusy(true);
    try {
      const result = await api.autoDescribeProfile(profile.name, true);
      if (!result.ok) throw new Error(result.reason ?? k.automationFailed);
      setProfiles((current) => current.map((item) => item.name === profile.name ? { ...item, description: result.description ?? "", description_auto: true } : item));
    } catch (cause) {
      setError(kanbanErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <GuiChatWorkspaceDialog busy={busy} description={k.settingsDescription} onClose={onClose} title={k.settings} wide>
      {error ? <div className="gui-chat-kanban-inline-error" role="alert">{error}</div> : null}
      {settings ? <div className="gui-chat-kanban-settings">
        <div className="gui-chat-kanban-form-grid">
          <ProfileSelect label={k.orchestratorProfile} onChange={(value) => setSettings({ ...settings, orchestrator_profile: value })} profiles={profiles} value={settings.orchestrator_profile} />
          <ProfileSelect label={k.defaultAssignee} onChange={(value) => setSettings({ ...settings, default_assignee: value })} profiles={profiles} value={settings.default_assignee} />
          <Toggle checked={settings.auto_decompose} label={k.autoDecompose} onChange={(checked) => setSettings({ ...settings, auto_decompose: checked })} />
          <Toggle checked={settings.auto_promote_children} label={k.autoPromoteChildren} onChange={(checked) => setSettings({ ...settings, auto_promote_children: checked })} />
        </div>
        <h3>{k.profileDescriptions}</h3>
        <div className="gui-chat-kanban-profile-list">
          {profiles.map((profile) => <ProfileDescription busy={busy} key={profile.name} onAuto={() => void autoDescribe(profile)} onSave={(description) => void saveDescription(profile, description)} profile={profile} />)}
        </div>
      </div> : <div className="gui-chat-kanban-loading">{k.loading}</div>}
      <div className="gui-chat-workspace-dialog-actions"><button disabled={busy} onClick={onClose} type="button">{t.common.cancel}</button><button className="is-primary" disabled={busy || !settings} onClick={() => void save()} type="button">{k.save}</button></div>
    </GuiChatWorkspaceDialog>
  );
}

function ProfileSelect({ label, onChange, profiles, value }: { label: string; onChange(value: string): void; profiles: KanbanProfile[]; value: string }) {
  return <label><span>{label}</span><select onChange={(event) => onChange(event.target.value)} value={value}><option value="">Default</option>{profiles.map((profile) => <option key={profile.name} value={profile.name}>{profile.name}</option>)}</select></label>;
}

function Toggle({ checked, label, onChange }: { checked: boolean; label: string; onChange(checked: boolean): void }) {
  return <label className="gui-chat-kanban-checkbox"><input checked={checked} onChange={(event) => onChange(event.target.checked)} type="checkbox" /><span>{label}</span></label>;
}

function ProfileDescription({ busy, onAuto, onSave, profile }: { busy: boolean; onAuto(): void; onSave(value: string): void; profile: KanbanProfile }) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  const [value, setValue] = useState(profile.description);
  return <div className="gui-chat-kanban-profile"><div><strong>{profile.name}</strong><span>{profile.provider} · {profile.model || "—"} · {profile.skill_count} {k.skills}</span></div><textarea onChange={(event) => setValue(event.target.value)} value={value} /><div><button disabled={busy} onClick={() => onSave(value)} type="button">{k.save}</button><button disabled={busy} onClick={onAuto} type="button"><WandSparkles />{k.autoDescribe}</button></div></div>;
}
