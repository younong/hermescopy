import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  Check,
  Cpu,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-react";

import {
  api,
  type ModelRegistration,
  type ModelRegistrationChatCatalogProvider,
  type ModelRegistrationKind,
  type ModelRegistrationMediaCatalogProvider,
  type ModelRegistrationRequest,
  type ModelRegistrationSource,
  type ModelRegistrationsResponse,
} from "@/lib/api";

import type { GuiChatModelSwitchResponse } from "../api";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";

interface GuiChatModelsPaneProps {
  busy: boolean;
  canSwitchChat: boolean;
  currentModel?: string;
  currentProvider?: string;
  onSwitchChat(
    registration: ModelRegistration,
    confirmExpensiveModel?: boolean,
    persistGlobally?: boolean,
  ): Promise<GuiChatModelSwitchResponse>;
}

interface RegistrationFormState {
  apiKey: string;
  apiMode: string;
  baseUrl: string;
  contextLength: string;
  kind: ModelRegistrationKind;
  model: string;
  name: string;
  provider: string;
  source: ModelRegistrationSource;
  useGateway: boolean;
}

interface PendingChatSwitch {
  message: string;
  persistGlobally: boolean;
  registration: ModelRegistration;
}

const EMPTY_FORM: RegistrationFormState = {
  apiKey: "",
  apiMode: "openai",
  baseUrl: "",
  contextLength: "",
  kind: "chat",
  model: "",
  name: "",
  provider: "",
  source: "catalog",
  useGateway: false,
};

const KIND_LABELS: Record<ModelRegistrationKind, string> = {
  chat: "Chat",
  image: "Image",
  video: "Video",
};

function registrationRequestFromForm(
  form: RegistrationFormState,
): ModelRegistrationRequest {
  const request: ModelRegistrationRequest = {
    kind: form.kind,
    model: form.model.trim(),
    name: form.name.trim(),
    source: form.source,
  };
  if (form.kind === "chat" && form.source === "custom") {
    request.api_key = form.apiKey.trim();
    request.api_mode = form.apiMode.trim() || "openai";
    request.base_url = form.baseUrl.trim();
    const contextLength = Number.parseInt(form.contextLength, 10);
    if (Number.isInteger(contextLength) && contextLength > 0) {
      request.context_length = contextLength;
    }
  } else {
    request.provider = form.provider;
    if (form.kind !== "chat") request.use_gateway = form.useGateway;
  }
  return request;
}

export function GuiChatModelsPane({
  busy,
  canSwitchChat,
  currentModel,
  currentProvider,
  onSwitchChat,
}: GuiChatModelsPaneProps) {
  const [data, setData] = useState<ModelRegistrationsResponse | null>(null);
  const [catalogs, setCatalogs] = useState<
    Partial<
      Record<
        ModelRegistrationKind,
        Array<
          ModelRegistrationChatCatalogProvider | ModelRegistrationMediaCatalogProvider
        >
      >
    >
  >({});
  const [catalogsLoading, setCatalogsLoading] = useState<
    Partial<Record<ModelRegistrationKind, boolean>>
  >({});
  const [selectedKind, setSelectedKind] = useState<ModelRegistrationKind>("chat");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ModelRegistration | null>(null);
  const [form, setForm] = useState<RegistrationFormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [workingId, setWorkingId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ModelRegistration | null>(null);
  const [pendingChatSwitch, setPendingChatSwitch] = useState<PendingChatSwitch | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getModelRegistrations());
    } catch (cause) {
      setError(errorMessage(cause));
      setData((current) => current ?? { active: emptyActive(), registrations: [] });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void api.getModelRegistrations().then((response) => {
      if (!cancelled) setData(response);
    }).catch((cause: unknown) => {
      if (!cancelled) {
        setError(errorMessage(cause));
        setData({ active: emptyActive(), registrations: [] });
      }
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (busy) setPendingChatSwitch(null);
  }, [busy]);

  const loadCatalog = useCallback(async (kind: ModelRegistrationKind) => {
    if (catalogs[kind] || catalogsLoading[kind]) return;
    setCatalogsLoading((current) => ({ ...current, [kind]: true }));
    try {
      const response = await api.getModelRegistrationCatalog(kind, );
      setCatalogs((current) => ({ ...current, [kind]: response.providers }));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setCatalogsLoading((current) => ({ ...current, [kind]: false }));
    }
  }, [catalogs, catalogsLoading, ]);

  const visibleRegistrations = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return (data?.registrations ?? []).filter((registration) => {
      if (registration.kind !== selectedKind) return false;
      if (!normalized) return true;
      return `${registration.name} ${registration.provider} ${registration.model} ${registration.source}`
        .toLocaleLowerCase()
        .includes(normalized);
    });
  }, [data, query, selectedKind]);

  const providers = catalogs[form.kind] ?? [];
  const models = useMemo(() => {
    if (form.source === "custom") return [];
    if (form.kind === "chat") {
      const provider = (providers as ModelRegistrationChatCatalogProvider[]).find(
        (item) => item.slug === form.provider,
      );
      return provider?.models.map((id) => ({ id, label: id })) ?? [];
    }
    const provider = (providers as ModelRegistrationMediaCatalogProvider[]).find(
      (item) => item.provider === form.provider,
    );
    return provider?.models.map((item) => ({
      id: item.id,
      label: item.display || item.id,
    })) ?? [];
  }, [form.kind, form.provider, form.source, providers]);

  const openCreate = () => {
    setEditing(null);
    setForm({ ...EMPTY_FORM, kind: selectedKind, source: "catalog" });
    setFormOpen(true);
    void loadCatalog(selectedKind);
  };

  const openEdit = (registration: ModelRegistration) => {
    setEditing(registration);
    setForm({
      ...EMPTY_FORM,
      kind: registration.kind,
      model: registration.model,
      name: registration.name,
      provider: registration.source === "catalog" ? registration.provider : "",
      source: registration.source,
      useGateway: registration.use_gateway,
    });
    setFormOpen(true);
    if (registration.source === "catalog") void loadCatalog(registration.kind);
  };

  const closeForm = () => {
    if (saving) return;
    setFormOpen(false);
    setEditing(null);
    setForm(EMPTY_FORM);
  };

  const updateKind = (kind: ModelRegistrationKind) => {
    void loadCatalog(kind);
    setForm((current) => ({
      ...current,
      kind,
      model: "",
      provider: "",
      source: kind === "chat" ? current.source : "catalog",
      useGateway: false,
    }));
  };

  const updateSource = (source: ModelRegistrationSource) => {
    if (source === "catalog") void loadCatalog(form.kind);
    setForm((current) => ({ ...current, model: "", provider: "", source }));
  };

  const updateProvider = (provider: string) => {
    let model = "";
    if (form.kind !== "chat") {
      const selected = (providers as ModelRegistrationMediaCatalogProvider[]).find(
        (item) => item.provider === provider,
      );
      model = selected?.default_model || selected?.models[0]?.id || "";
    }
    setForm((current) => ({ ...current, model, provider }));
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    const validationError = validateForm(form);
    if (validationError) {
      setError(validationError);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const request = registrationRequestFromForm(form);
      if (editing) {
        await api.updateModelRegistration(editing.id, request);
      } else {
        await api.createModelRegistration(request);
      }
      setFormOpen(false);
      setEditing(null);
      setForm(EMPTY_FORM);
      await load();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSaving(false);
    }
  };

  const applyChat = async (
    registration: ModelRegistration,
    persistGlobally: boolean,
    confirmExpensiveModel = false,
  ) => {
    if (workingId || busy || !canSwitchChat) return;
    setWorkingId(registration.id);
    setError(null);
    try {
      const result = await onSwitchChat(
        registration,
        confirmExpensiveModel,
        persistGlobally,
      );
      if (result.confirm_required) {
        setPendingChatSwitch({
          message: result.confirm_message || result.warning || "This model has unusually high known pricing.",
          persistGlobally,
          registration,
        });
        return;
      }
      await load();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setWorkingId(null);
    }
  };

  const activateMedia = async (registration: ModelRegistration) => {
    if (workingId) return;
    setWorkingId(registration.id);
    setError(null);
    try {
      await api.activateModelRegistration(registration.id, );
      await load();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setWorkingId(null);
    }
  };

  const remove = async () => {
    if (!pendingDelete || workingId) return;
    setWorkingId(pendingDelete.id);
    setError(null);
    try {
      await api.deleteModelRegistration(pendingDelete.id);
      setPendingDelete(null);
      await load();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setWorkingId(null);
    }
  };

  return (
    <section aria-label="Models" className="gui-chat-workspace-pane" data-models-pane>
      <header className="gui-chat-workspace-toolbar">
        <button className="gui-chat-workspace-primary-button" onClick={openCreate} type="button">
          <Plus aria-hidden />
          Add model
        </button>
        <button
          aria-label="Refresh models"
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
          <h1>Models</h1>
          <p>Manage models and choose which ones power conversations and generation.</p>
        </div>
        <label className="gui-chat-workspace-search">
          <Search aria-hidden />
          <input
            aria-label="Search models"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search models"
            value={query}
          />
        </label>
      </div>

      <div aria-label="Model type" className="gui-chat-model-tabs" role="tablist">
        {(Object.keys(KIND_LABELS) as ModelRegistrationKind[]).map((kind) => (
          <button
            aria-selected={selectedKind === kind}
            className={selectedKind === kind ? "is-active" : undefined}
            key={kind}
            onClick={() => setSelectedKind(kind)}
            role="tab"
            type="button"
          >
            {KIND_LABELS[kind]}
          </button>
        ))}
      </div>

      {error || busy ? (
        <div className="gui-chat-workspace-feedback is-error" role="alert">
          {error ?? "Stop the current response before switching chat models."}
        </div>
      ) : null}

      <div className="gui-chat-workspace-list">
        {data === null && loading ? (
          <div className="gui-chat-workspace-empty" role="status">Loading models…</div>
        ) : visibleRegistrations.length === 0 ? (
          <div className="gui-chat-workspace-empty">
            <Cpu aria-hidden />
            <strong>{query.trim() ? "No matching models" : `No ${selectedKind} models yet`}</strong>
            <span>{query.trim() ? "Try a different search." : "Add a model to make it available here."}</span>
          </div>
        ) : visibleRegistrations.map((registration) => {
          const current = registration.kind === "chat"
            && registration.provider === currentProvider
            && registration.model === currentModel;
          const configured = data?.active[registration.kind]?.registration_id === registration.id;
          const active = registration.kind === "chat" ? current : configured;
          const working = workingId === registration.id;
          return (
            <article className="gui-chat-workspace-row gui-chat-model-row" key={registration.id}>
              <div className="gui-chat-workspace-copy">
                <div className="gui-chat-workspace-title">
                  <span>{registration.name}</span>
                  <ModelBadge>{KIND_LABELS[registration.kind]}</ModelBadge>
                  <ModelBadge>{registration.source === "custom" ? "Custom" : "Catalog"}</ModelBadge>
                  {current ? <ModelBadge active>Current conversation</ModelBadge> : null}
                  {registration.kind === "chat" && configured ? <ModelBadge active>Default</ModelBadge> : null}
                  {registration.kind !== "chat" && active ? <ModelBadge active>Active</ModelBadge> : null}
                  {registration.source === "custom" ? (
                    <ModelBadge warning={!registration.credential_configured}>
                      {registration.credential_configured ? "Credential ready" : "Credential missing"}
                    </ModelBadge>
                  ) : null}
                </div>
                <p>{registration.provider} · {registration.model}</p>
              </div>
              <div className="gui-chat-workspace-actions gui-chat-model-actions">
                {registration.kind === "chat" ? (
                  <>
                    <button
                      className="gui-chat-model-action"
                      disabled={!canSwitchChat || busy || working || current}
                      onClick={() => void applyChat(registration, false)}
                      title={!canSwitchChat ? "Start or open a conversation to switch its model." : undefined}
                      type="button"
                    >
                      {current ? "In use" : "Use"}
                    </button>
                    <button
                      className="gui-chat-model-action"
                      disabled={!canSwitchChat || busy || working || (current && configured)}
                      onClick={() => void applyChat(registration, true)}
                      title={!canSwitchChat ? "Start or open a conversation to set the default." : undefined}
                      type="button"
                    >
                      {configured ? "Default" : "Use as default"}
                    </button>
                  </>
                ) : !active ? (
                  <button
                    className="gui-chat-model-action"
                    disabled={working}
                    onClick={() => void activateMedia(registration)}
                    type="button"
                  >
                    {working ? "Activating…" : "Activate"}
                  </button>
                ) : null}
                <button
                  aria-label={`Edit ${registration.name}`}
                  className="gui-chat-workspace-icon-button"
                  disabled={working}
                  onClick={() => openEdit(registration)}
                  type="button"
                >
                  <Pencil aria-hidden />
                </button>
                <button
                  aria-label={`Delete ${registration.name}`}
                  className="gui-chat-workspace-icon-button is-destructive"
                  disabled={working || configured}
                  onClick={() => setPendingDelete(registration)}
                  title={configured ? "Switch the active model before deleting this registration." : undefined}
                  type="button"
                >
                  <Trash2 aria-hidden />
                </button>
              </div>
            </article>
          );
        })}
      </div>

      {formOpen ? (
        <RegistrationDialog
          catalogLoading={Boolean(catalogsLoading[form.kind])}
          editing={editing}
          form={form}
          models={models}
          onClose={closeForm}
          onKindChange={updateKind}
          onProviderChange={updateProvider}
          onSourceChange={updateSource}
          onSubmit={save}
          providers={providers}
          saving={saving}
          setForm={setForm}
        />
      ) : null}

      {pendingDelete ? (
        <GuiChatWorkspaceDialog
          busy={workingId === pendingDelete.id}
          description={`This permanently removes ${pendingDelete.name} and its private provider configuration. This action cannot be undone.`}
          onClose={() => setPendingDelete(null)}
          title={`Delete ${pendingDelete.name}?`}
        >
          <div className="gui-chat-workspace-dialog-actions">
            <button disabled={workingId === pendingDelete.id} onClick={() => setPendingDelete(null)} type="button">Cancel</button>
            <button className="is-destructive" disabled={workingId === pendingDelete.id} onClick={() => void remove()} type="button">
              {workingId === pendingDelete.id ? "Deleting…" : "Delete"}
            </button>
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}

      {pendingChatSwitch ? (
        <GuiChatWorkspaceDialog
          busy={workingId === pendingChatSwitch.registration.id}
          description={pendingChatSwitch.message}
          onClose={() => setPendingChatSwitch(null)}
          title="Expensive model warning"
        >
          <div className="gui-chat-workspace-dialog-actions">
            <button onClick={() => setPendingChatSwitch(null)} type="button">Cancel</button>
            <button
              className="is-destructive"
              onClick={() => {
                const pending = pendingChatSwitch;
                setPendingChatSwitch(null);
                void applyChat(pending.registration, pending.persistGlobally, true);
              }}
              type="button"
            >
              Switch anyway
            </button>
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}
    </section>
  );
}

function ModelBadge({
  active = false,
  children,
  warning = false,
}: {
  active?: boolean;
  children: ReactNode;
  warning?: boolean;
}) {
  return (
    <span className={`gui-chat-model-badge${active ? " is-active" : ""}${warning ? " is-warning" : ""}`}>
      {active ? <Check aria-hidden /> : null}
      {children}
    </span>
  );
}

function RegistrationDialog({
  catalogLoading,
  editing,
  form,
  models,
  onClose,
  onKindChange,
  onProviderChange,
  onSourceChange,
  onSubmit,
  providers,
  saving,
  setForm,
}: {
  catalogLoading: boolean;
  editing: ModelRegistration | null;
  form: RegistrationFormState;
  models: Array<{ id: string; label: string }>;
  onClose(): void;
  onKindChange(kind: ModelRegistrationKind): void;
  onProviderChange(provider: string): void;
  onSourceChange(source: ModelRegistrationSource): void;
  onSubmit(event: FormEvent): void;
  providers: Array<ModelRegistrationChatCatalogProvider | ModelRegistrationMediaCatalogProvider>;
  saving: boolean;
  setForm: React.Dispatch<React.SetStateAction<RegistrationFormState>>;
}) {
  const selectedMediaProvider = form.kind === "chat"
    ? undefined
    : (providers as ModelRegistrationMediaCatalogProvider[]).find(
      (item) => item.provider === form.provider,
    );
  return (
    <GuiChatWorkspaceDialog
      busy={saving}
      description={editing ? "Update this registered model." : "Register a model for chat, image, or video generation."}
      onClose={onClose}
      title={editing ? "Edit model" : "Add model"}
      wide
    >
      <form className="gui-chat-model-form" onSubmit={onSubmit}>
        <FormField label="Name">
          <input
            aria-label="Model name"
            autoFocus
            disabled={saving}
            onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
            placeholder="My model"
            value={form.name}
          />
        </FormField>
        <div className="gui-chat-skills-editor-grid">
          <FormField label="Type">
            <select
              aria-label="Model type"
              disabled={saving || editing !== null}
              onChange={(event) => onKindChange(event.target.value as ModelRegistrationKind)}
              value={form.kind}
            >
              {(Object.keys(KIND_LABELS) as ModelRegistrationKind[]).map((kind) => <option key={kind} value={kind}>{KIND_LABELS[kind]}</option>)}
            </select>
          </FormField>
          <FormField label="Source">
            <select
              aria-label="Model source"
              disabled={saving || editing !== null || form.kind !== "chat"}
              onChange={(event) => onSourceChange(event.target.value as ModelRegistrationSource)}
              value={form.source}
            >
              <option value="catalog">Catalog</option>
              {form.kind === "chat" ? <option value="custom">Custom endpoint</option> : null}
            </select>
          </FormField>
        </div>

        {form.source === "catalog" ? (
          <>
            {catalogLoading ? <div className="gui-chat-model-form-note">Loading providers…</div> : null}
            <FormField label="Provider">
              <select
                aria-label="Model provider"
                disabled={saving || catalogLoading}
                onChange={(event) => onProviderChange(event.target.value)}
                value={form.provider}
              >
                <option value="">Select a provider</option>
                {providers.map((provider) => {
                  const id = "slug" in provider ? provider.slug : provider.provider;
                  return <option key={id} value={id}>{provider.name}</option>;
                })}
              </select>
            </FormField>
            <FormField label="Model">
              <select
                aria-label="Model"
                disabled={saving || !form.provider}
                onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))}
                value={form.model}
              >
                <option value="">Select a model</option>
                {models.map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}
              </select>
            </FormField>
            {form.kind !== "chat" ? (
              <label className="gui-chat-model-checkbox">
                <input
                  checked={form.useGateway}
                  disabled={saving}
                  onChange={(event) => setForm((current) => ({ ...current, useGateway: event.target.checked }))}
                  type="checkbox"
                />
                <span>Use the configured gateway for this provider</span>
              </label>
            ) : null}
            {selectedMediaProvider && !selectedMediaProvider.available ? (
              <div className="gui-chat-model-form-note is-warning">This provider is missing its required credential.</div>
            ) : null}
          </>
        ) : (
          <>
            {editing ? (
              <div className="gui-chat-model-form-note is-warning">
                Custom endpoint URLs and API keys are write-only. Re-enter the endpoint; leave the API key blank to keep the saved credential.
              </div>
            ) : null}
            <FormField label="Model">
              <input aria-label="Model" disabled={saving} onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))} placeholder="model-id" value={form.model} />
            </FormField>
            <FormField label="Base URL">
              <input aria-label="Base URL" disabled={saving} onChange={(event) => setForm((current) => ({ ...current, baseUrl: event.target.value }))} placeholder="https://api.example.com/v1" value={form.baseUrl} />
            </FormField>
            <div className="gui-chat-skills-editor-grid">
              <FormField label="API mode">
                <input aria-label="API mode" disabled={saving} onChange={(event) => setForm((current) => ({ ...current, apiMode: event.target.value }))} value={form.apiMode} />
              </FormField>
              <FormField label="Context length">
                <input aria-label="Context length" disabled={saving} inputMode="numeric" min="1" onChange={(event) => setForm((current) => ({ ...current, contextLength: event.target.value }))} type="number" value={form.contextLength} />
              </FormField>
            </div>
            <FormField label={editing ? "API key (leave blank to keep current)" : "API key (optional)"}>
              <input aria-label="API key" autoComplete="new-password" disabled={saving} onChange={(event) => setForm((current) => ({ ...current, apiKey: event.target.value }))} type="password" value={form.apiKey} />
            </FormField>
          </>
        )}

        <div className="gui-chat-workspace-dialog-actions">
          <button disabled={saving} onClick={onClose} type="button">Cancel</button>
          <button className="is-primary" disabled={saving} type="submit">{saving ? "Saving…" : "Save model"}</button>
        </div>
      </form>
    </GuiChatWorkspaceDialog>
  );
}

function FormField({ children, label }: { children: ReactNode; label: string }) {
  return <label className="gui-chat-model-field"><span>{label}</span>{children}</label>;
}

function validateForm(form: RegistrationFormState): string | null {
  if (!form.name.trim()) return "Model name is required.";
  if (form.kind === "chat" && form.source === "custom") {
    if (!form.model.trim()) return "Model is required.";
    if (!form.baseUrl.trim()) return "Base URL is required.";
    return null;
  }
  if (!form.provider) return "Provider is required.";
  if (!form.model) return "Model is required.";
  return null;
}

function emptyActive(): ModelRegistrationsResponse["active"] {
  return {
    chat: { model: "", provider: "", registration_id: null },
    image: { model: "", provider: "", registration_id: null },
    video: { model: "", provider: "", registration_id: null },
  };
}

function errorMessage(cause: unknown): string {
  if (!(cause instanceof Error)) return String(cause);
  const jsonStart = cause.message.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const payload = JSON.parse(cause.message.slice(jsonStart)) as { detail?: unknown };
      if (typeof payload.detail === "string") return payload.detail;
    } catch {
      // Use the original response when it is not JSON.
    }
  }
  return cause.message;
}
