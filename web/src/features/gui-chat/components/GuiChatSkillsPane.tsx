import { useCallback, useEffect, useMemo, useState } from "react";
import { Plus, RefreshCw, Search, Sparkles, Trash2 } from "lucide-react";
import { guiChatTranslations, useI18n } from "@/i18n";
import { api, type SkillInfo } from "@/lib/api";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";

const CREATE_TEMPLATE = `---
name: my-skill
description: One-line description of when to use this skill.
---

# My Skill

Numbered steps, exact commands, and pitfalls go here.
`;

export function GuiChatSkillsPane() {
  const { t } = useI18n();
  const text = guiChatTranslations(t).skills;
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingSkill, setSavingSkill] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<SkillInfo | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await api.getSkills();
      setSkills([...rows].sort((a, b) => a.name.localeCompare(b.name)));
    } catch (cause) {
      setError(errorMessage(cause));
      setSkills((current) => current ?? []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .getSkills()
      .then((rows) => {
        if (!cancelled) setSkills([...rows].sort((a, b) => a.name.localeCompare(b.name)));
      })
      .catch((cause) => {
        if (!cancelled) {
          setError(errorMessage(cause));
          setSkills([]);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const visibleSkills = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return skills ?? [];
    return (skills ?? []).filter((skill) =>
      [skill.name, skill.description, skill.category].some((value) =>
        String(value || "").toLowerCase().includes(normalized),
      ),
    );
  }, [query, skills]);

  const toggleSkill = async (skill: SkillInfo, enabled: boolean) => {
    setSavingSkill(skill.name);
    setError(null);
    try {
      await api.toggleSkill(skill.name, enabled, );
      setSkills((current) =>
        current?.map((row) => row.name === skill.name ? { ...row, enabled } : row) ?? current,
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSavingSkill(null);
    }
  };

  const deleteSkill = async () => {
    if (!pendingDelete) return;
    const name = pendingDelete.name;
    setSavingSkill(name);
    setError(null);
    try {
      await api.deleteSkill(name, );
      setSkills((current) => current?.filter((skill) => skill.name !== name) ?? current);
      setPendingDelete(null);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSavingSkill(null);
    }
  };

  return (
    <section aria-label={text.title} className="gui-chat-workspace-pane" data-skills-pane>
      <header className="gui-chat-workspace-toolbar">
        <button className="gui-chat-workspace-primary-button" onClick={() => setCreateOpen(true)} type="button">
          <Plus aria-hidden />{text.newSkill}        </button>
        <button
          aria-label={text.refresh}
          className="gui-chat-workspace-icon-button"
          disabled={loading}
          onClick={() => void load()}
          type="button"
        >
          <RefreshCw aria-hidden className={loading ? "animate-spin" : ""} />
        </button>
      </header>

      <div className="gui-chat-workspace-heading">
        <div>
          <h1>{text.title}</h1>
          <p>{text.description}</p>
        </div>
        <label className="gui-chat-workspace-search">
          <Search aria-hidden />
          <input
            aria-label={text.search}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={text.search}
            value={query}
          />
        </label>
      </div>

      {error ? <div className="gui-chat-workspace-feedback is-error" role="alert">{error}</div> : null}

      <div className="gui-chat-workspace-list">
        {skills === null && loading ? (
          <div className="gui-chat-workspace-empty" role="status">{text.loading}</div>
        ) : visibleSkills.length === 0 ? (
          <div className="gui-chat-workspace-empty">
            <Sparkles aria-hidden />
            <strong>{query.trim() ? text.noMatching : text.none}</strong>
            <span>{query.trim() ? text.differentSearch : text.createHint}</span>
          </div>
        ) : (
          visibleSkills.map((skill) => (
            <article className="gui-chat-workspace-row" key={skill.name}>
              <div className="gui-chat-workspace-copy">
                <div className="gui-chat-workspace-title">
                  <span>{skill.name}</span>
                  {skill.category ? <span className="gui-chat-workspace-badge">{skill.category}</span> : null}
                </div>
                <p>{skill.description || text.noDescription}</p>
              </div>
              <div className="gui-chat-workspace-actions">
                <button
                  aria-checked={skill.enabled}
                  aria-label={(skill.enabled ? text.disableNamed : text.enableNamed).replace("{name}", skill.name)}
                  className="gui-chat-skill-switch"
                  disabled={savingSkill === skill.name}
                  onClick={() => void toggleSkill(skill, !skill.enabled)}
                  role="switch"
                  type="button"
                >
                  <span />
                </button>
                <button
                  aria-label={text.deleteNamed.replace("{name}", skill.name)}
                  className="gui-chat-workspace-icon-button is-destructive"
                  disabled={savingSkill === skill.name}
                  onClick={() => setPendingDelete(skill)}
                  type="button"
                >
                  <Trash2 aria-hidden />
                </button>
              </div>
            </article>
          ))
        )}
      </div>

      {createOpen ? (
        <CreateSkillDialog
          busy={savingSkill === "__create__"}
          onClose={() => setCreateOpen(false)}
          onCreate={async (name, category, content) => {
            setSavingSkill("__create__");
            setError(null);
            try {
              await api.createSkill({ name, category: category || undefined, content }, );
              await load();
              setCreateOpen(false);
            } catch (cause) {
              throw new Error(errorMessage(cause));
            } finally {
              setSavingSkill(null);
            }
          }}
        />
      ) : null}

      {pendingDelete ? (
        <GuiChatWorkspaceDialog
          busy={savingSkill === pendingDelete.name}
          description={text.deleteDescription}
          onClose={() => setPendingDelete(null)}
          title={text.deleteTitle.replace("{name}", pendingDelete.name)}
        >
          <div className="gui-chat-workspace-dialog-actions">
            <button disabled={savingSkill === pendingDelete.name} onClick={() => setPendingDelete(null)} type="button">{t.common.cancel}</button>
            <button className="is-destructive" disabled={savingSkill === pendingDelete.name} onClick={() => void deleteSkill()} type="button">
              {savingSkill === pendingDelete.name ? text.deleting : t.common.delete}
            </button>
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}
    </section>
  );
}

function CreateSkillDialog({
  busy,
  onClose,
  onCreate,
}: {
  busy: boolean;
  onClose: () => void;
  onCreate: (name: string, category: string, content: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const text = guiChatTranslations(t).skills;
  const [name, setName] = useState("");
  const [category, setCategory] = useState("");
  const [content, setContent] = useState(CREATE_TEMPLATE);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const trimmedName = name.trim();
    if (!trimmedName) {
      setError(text.nameRequired);
      return;
    }
    if (!content.trim()) {
      setError(text.contentRequired);
      return;
    }
    setError(null);
    try {
      await onCreate(trimmedName, category.trim(), content);
    } catch (cause) {
      setError(errorMessage(cause));
    }
  };

  return (
    <GuiChatWorkspaceDialog
      busy={busy}
      description={text.addDescription}
      onClose={onClose}
      title={text.newSkill}
      wide
    >
      <div className="gui-chat-skills-editor-grid">
        <label>
          <span>{text.name}</span>
          <input aria-label={text.name} autoFocus disabled={busy} onChange={(event) => setName(event.target.value)} placeholder={text.namePlaceholder} value={name} />
        </label>
        <label>
          <span>{text.categoryOptional}</span>
          <input aria-label={text.category} disabled={busy} onChange={(event) => setCategory(event.target.value)} placeholder={text.categoryPlaceholder} value={category} />
        </label>
      </div>
      <label className="gui-chat-skills-editor-content">
        <span>SKILL.md</span>
        <textarea aria-label="SKILL.md" disabled={busy} onChange={(event) => setContent(event.target.value)} spellCheck={false} value={content} />
      </label>
      {error ? <div className="gui-chat-skills-editor-error" role="alert">{error}</div> : null}
      <div className="gui-chat-workspace-dialog-actions">
        <button disabled={busy} onClick={onClose} type="button">{t.common.cancel}</button>
        <button className="is-primary" disabled={busy} onClick={() => void submit()} type="button">
          {busy ? text.creating : text.createSkill}
        </button>
      </div>
    </GuiChatWorkspaceDialog>
  );
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}
