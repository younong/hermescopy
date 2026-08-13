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
  type ModelRegistrationCapabilityCatalogProvider,
  type ModelRegistrationChatCatalogProvider,
  type ModelRegistrationKind,
  type ModelRegistrationRequest,
  type ModelRegistrationSource,
  type ModelRegistrationsResponse,
} from "@/lib/api";

import { guiChatTranslations, useI18n } from "@/i18n";
import type { GuiChatModelSwitchResponse } from "../api";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";

interface GuiChatModelsPaneProps {
  busy: boolean;
  canSwitchChat: boolean;
  currentModel?: string;
  currentProvider?: string;
  onActivateCode?: (registration: ModelRegistration) => Promise<void>;
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

function hasCatalog(kind: ModelRegistrationKind): boolean {
  return ["chat", "code", "image", "video", "voice", "vector"].includes(kind);
}

function defaultSource(kind: ModelRegistrationKind): ModelRegistrationSource {
  return kind === "voice" || kind === "vector" ? "manual" : "catalog";
}

function isActivatable(kind: ModelRegistrationKind): kind is "image" | "video" {
  return kind === "image" || kind === "video";
}

function supportsGateway(kind: ModelRegistrationKind): kind is "image" | "video" {
  return kind === "image" || kind === "video";
}

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
    if (supportsGateway(form.kind)) {
      request.use_gateway = form.useGateway;
    }
  }
  return request;
}

export function GuiChatModelsPane({
  busy,
  canSwitchChat,
  currentModel,
  currentProvider,
  onActivateCode,
  onSwitchChat,
}: GuiChatModelsPaneProps) {
  const { t } = useI18n();
  const text = guiChatTranslations(t).models;
  const kindLabels = text.kinds;
  const [data, setData] = useState<ModelRegistrationsResponse | null>(null);
  const [catalogs, setCatalogs] = useState<
    Partial<
      Record<
        ModelRegistrationKind,
        Array<
          ModelRegistrationChatCatalogProvider | ModelRegistrationCapabilityCatalogProvider
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
    if (!hasCatalog(kind) || catalogs[kind] || catalogsLoading[kind]) return;
    setCatalogsLoading((current) => ({ ...current, [kind]: true }));
    try {
      const response = await api.getModelRegistrationCatalog(kind);
      setCatalogs((current) => ({ ...current, [kind]: response.providers }));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setCatalogsLoading((current) => ({ ...current, [kind]: false }));
    }
  }, [catalogs, catalogsLoading]);

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
    if (form.source !== "catalog") return [];
    if (form.kind === "chat") {
      const provider = (providers as ModelRegistrationChatCatalogProvider[]).find(
        (item) => item.slug === form.provider,
      );
      return provider?.models.map((id) => ({ id, label: id })) ?? [];
    }
    const provider = (providers as ModelRegistrationCapabilityCatalogProvider[]).find(
      (item) => item.provider === form.provider,
    );
    return provider?.models.map((item) => ({
      id: item.id,
      label: `${item.display || item.id}${item.capability ? ` · ${String(item.capability).toUpperCase()}` : ""}`,
    })) ?? [];
  }, [form.kind, form.provider, form.source, providers]);

  const openCreate = () => {
    setEditing(null);
    setForm({
      ...EMPTY_FORM,
      kind: selectedKind,
      source: defaultSource(selectedKind),
    });
    setFormOpen(true);
    if (defaultSource(selectedKind) === "catalog") void loadCatalog(selectedKind);
  };

  const openEdit = (registration: ModelRegistration) => {
    setEditing(registration);
    setForm({
      ...EMPTY_FORM,
      kind: registration.kind,
      model: registration.model,
      name: registration.name,
      provider: registration.source === "custom" ? "" : registration.provider,
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
      source: defaultSource(kind),
      useGateway: false,
    }));
  };

  const updateSource = (source: ModelRegistrationSource) => {
    if (source === "catalog") void loadCatalog(form.kind);
    setForm((current) => ({ ...current, model: "", provider: "", source }));
  };

  const updateProvider = (provider: string) => {
    let model = "";
    if (form.kind === "chat") {
      const selected = (providers as ModelRegistrationChatCatalogProvider[]).find(
        (item) => item.slug === provider,
      );
      model = selected?.models[0] ?? "";
    } else {
      const selected = (providers as ModelRegistrationCapabilityCatalogProvider[]).find(
        (item) => item.provider === provider,
      );
      model = selected?.default_model || selected?.models[0]?.id || "";
    }
    setForm((current) => ({ ...current, model, provider }));
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    const validationError = validateForm(form, text);
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
          message: result.confirm_message || result.warning || text.expensiveWarning,
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
      await api.activateModelRegistration(registration.id);
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
    <section aria-label={text.title} className="gui-chat-workspace-pane" data-models-pane>
      <header className="gui-chat-workspace-toolbar">
        <button className="gui-chat-workspace-primary-button" onClick={openCreate} type="button">
          <Plus aria-hidden />{text.addModel}        </button>
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

      <div aria-label={text.modelType} className="gui-chat-model-tabs" role="tablist">
        {(Object.keys(kindLabels) as ModelRegistrationKind[])
          .map((kind) => (
            <button
              aria-selected={selectedKind === kind}
              className={selectedKind === kind ? "is-active" : undefined}
              key={kind}
              onClick={() => setSelectedKind(kind)}
              role="tab"
              type="button"
            >
              {kindLabels[kind]}
            </button>
          ))}
      </div>

      {error || busy ? (
        <div className="gui-chat-workspace-feedback is-error" role="alert">
          {error ?? text.stopBeforeSwitching}
        </div>
      ) : null}

      <div className="gui-chat-workspace-list">
        {data === null && loading ? (
          <div className="gui-chat-workspace-empty" role="status">{text.loading}</div>
        ) : visibleRegistrations.length === 0 ? (
          <div className="gui-chat-workspace-empty">
            <Cpu aria-hidden />
            <strong>{query.trim() ? text.noMatching : text.noModels.replace("{kind}", kindLabels[selectedKind])}</strong>
            <span>{query.trim() ? text.differentSearch : text.addModelHint}</span>
          </div>
        ) : visibleRegistrations.map((registration) => {
          const current = registration.kind === "chat"
            && registration.provider === currentProvider
            && registration.model === currentModel;
          const configured = data?.active[registration.kind]?.registration_id === registration.id;
          const activatable = isActivatable(registration.kind);
          const working = workingId === registration.id;
          return (
            <article className="gui-chat-workspace-row gui-chat-model-row" key={registration.id}>
              <div className="gui-chat-workspace-copy">
                <div className="gui-chat-workspace-title">
                  <span>{registration.name}</span>
                  <ModelBadge>{kindLabels[registration.kind]}</ModelBadge>
                  <ModelBadge>{registration.scope === "admin" ? text.admin : text.mine}</ModelBadge>
                  <ModelBadge>{registration.source === "custom" ? text.custom : registration.source === "manual" ? text.manual : text.catalog}</ModelBadge>
                  {current ? <ModelBadge active>{text.currentConversation}</ModelBadge> : null}
                  {(registration.kind === "chat" || registration.kind === "code") && configured ? <ModelBadge active>{text.default}</ModelBadge> : null}
                  {activatable && configured ? <ModelBadge active>{text.active}</ModelBadge> : null}
                  {registration.source === "custom" ? (
                    <ModelBadge warning={!registration.credential_configured}>
                      {registration.credential_configured ? text.credentialReady : text.credentialMissing}
                    </ModelBadge>
                  ) : null}
                </div>
                <p>{registration.provider} · {registration.model}</p>
                {!registration.mutable ? <p>{text.managedByAdmin}</p> : null}
              </div>
              <div className="gui-chat-workspace-actions gui-chat-model-actions">
                {registration.kind === "chat" ? (
                  <>
                    <button
                      className="gui-chat-model-action"
                      disabled={!canSwitchChat || busy || working || current}
                      onClick={() => void applyChat(registration, false)}
                      title={!canSwitchChat ? text.startConversationToSwitch : undefined}
                      type="button"
                    >
                      {current ? text.inUse : text.use}
                    </button>
                    <button
                      className="gui-chat-model-action"
                      disabled={!canSwitchChat || busy || working || (current && configured)}
                      onClick={() => void applyChat(registration, true)}
                      title={!canSwitchChat ? text.startConversationForDefault : undefined}
                      type="button"
                    >
                      {configured ? text.default : text.useAsDefault}
                    </button>
                  </>
                ) : registration.kind === "code" ? (
                  <button
                    className="gui-chat-model-action"
                    disabled={working || !onActivateCode}
                    onClick={() => {
                      if (!onActivateCode) return;
                      setWorkingId(registration.id);
                      setError(null);
                      void onActivateCode(registration)
                        .then(() => load())
                        .catch((cause) => setError(errorMessage(cause)))
                        .finally(() => setWorkingId(null));
                    }}
                    title={!onActivateCode ? text.codeUnavailable : undefined}
                    type="button"
                  >
                    {working ? text.activating : configured ? text.default : text.useAsCodeModel}
                  </button>
                ) : activatable && !configured ? (
                  <button
                    className="gui-chat-model-action"
                    disabled={working}
                    onClick={() => void activateMedia(registration)}
                    type="button"
                  >
                    {working ? text.activating : text.activate}
                  </button>
                ) : null}
                {registration.mutable ? (
                  <>
                    <button
                      aria-label={text.editNamed.replace("{name}", registration.name)}
                      className="gui-chat-workspace-icon-button"
                      disabled={working}
                      onClick={() => openEdit(registration)}
                      type="button"
                    >
                      <Pencil aria-hidden />
                    </button>
                    <button
                      aria-label={text.deleteNamed.replace("{name}", registration.name)}
                      className="gui-chat-workspace-icon-button is-destructive"
                      disabled={working || configured}
                      onClick={() => setPendingDelete(registration)}
                      title={configured ? text.switchBeforeDelete : undefined}
                      type="button"
                    >
                      <Trash2 aria-hidden />
                    </button>
                  </>
                ) : null}
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
          text={text}
        />
      ) : null}

      {pendingDelete ? (
        <GuiChatWorkspaceDialog
          busy={workingId === pendingDelete.id}
          description={text.deleteDescription.replace("{name}", pendingDelete.name)}
          onClose={() => setPendingDelete(null)}
          title={text.deleteTitle.replace("{name}", pendingDelete.name)}
        >
          <div className="gui-chat-workspace-dialog-actions">
            <button disabled={workingId === pendingDelete.id} onClick={() => setPendingDelete(null)} type="button">{t.common.cancel}</button>
            <button className="is-destructive" disabled={workingId === pendingDelete.id} onClick={() => void remove()} type="button">
              {workingId === pendingDelete.id ? text.deleting : t.common.delete}
            </button>
          </div>
        </GuiChatWorkspaceDialog>
      ) : null}

      {pendingChatSwitch ? (
        <GuiChatWorkspaceDialog
          busy={workingId === pendingChatSwitch.registration.id}
          description={pendingChatSwitch.message}
          onClose={() => setPendingChatSwitch(null)}
          title={text.expensiveWarningTitle}
        >
          <div className="gui-chat-workspace-dialog-actions">
            <button onClick={() => setPendingChatSwitch(null)} type="button">{t.common.cancel}</button>
            <button
              className="is-destructive"
              onClick={() => {
                const pending = pendingChatSwitch;
                setPendingChatSwitch(null);
                void applyChat(pending.registration, pending.persistGlobally, true);
              }}
              type="button"
            >{text.switchAnyway}            </button>
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
  text,
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
  providers: Array<ModelRegistrationChatCatalogProvider | ModelRegistrationCapabilityCatalogProvider>;
  saving: boolean;
  setForm: React.Dispatch<React.SetStateAction<RegistrationFormState>>;
  text: ReturnType<typeof guiChatTranslations>["models"];
}) {
  const { t } = useI18n();
  const selectedMediaProvider = form.kind === "chat"
    ? undefined
    : (providers as ModelRegistrationCapabilityCatalogProvider[]).find(
      (item) => item.provider === form.provider,
    );
  return (
    <GuiChatWorkspaceDialog
      busy={saving}
      description={editing ? text.editDescription : text.addDescription}
      onClose={onClose}
      title={editing ? text.editModel : text.addModel}
      wide
    >
      <form className="gui-chat-model-form" onSubmit={onSubmit}>
        <FormField label={text.name}>
          <input
            aria-label={text.name}
            autoFocus
            disabled={saving}
            onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
            placeholder={text.namePlaceholder}
            value={form.name}
          />
        </FormField>
        <div className="gui-chat-skills-editor-grid">
          <FormField label={text.type}>
            <select
              aria-label={text.modelType}
              disabled={saving || editing !== null}
              onChange={(event) => onKindChange(event.target.value as ModelRegistrationKind)}
              value={form.kind}
            >
              {(Object.keys(text.kinds) as ModelRegistrationKind[]).map((kind) => <option key={kind} value={kind}>{text.kinds[kind]}</option>)}
            </select>
          </FormField>
          <FormField label={text.source}>
            <select
              aria-label={text.source}
              disabled={saving || editing !== null || form.kind === "voice" || form.kind === "vector"}
              onChange={(event) => onSourceChange(event.target.value as ModelRegistrationSource)}
              value={form.source}
            >
              {hasCatalog(form.kind) && form.kind !== "voice" && form.kind !== "vector" ? <option value="catalog">{text.catalog}</option> : null}
              {form.source === "manual" ? <option value="manual">{text.manual}</option> : null}
              {form.kind === "chat" ? <option value="custom">{text.customEndpoint}</option> : null}
            </select>
          </FormField>
        </div>

        {form.source !== "custom" ? (
          <>
            {form.source === "manual" ? (
              <>
                <FormField label={text.provider}>
                  <input aria-label={text.provider} disabled={saving} onChange={(event) => setForm((current) => ({ ...current, provider: event.target.value }))} placeholder="openai" value={form.provider} />
                </FormField>
                <FormField label={text.model}>
                  <input aria-label={text.model} disabled={saving} onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))} placeholder={form.kind === "voice" ? "gpt-4o-mini-tts" : "text-embedding-3-small"} value={form.model} />
                </FormField>
              </>
            ) : (
              <>
                {catalogLoading ? <div className="gui-chat-model-form-note">{text.loadingProviders}</div> : null}
                <FormField label={text.provider}>
                  <select
                    aria-label={text.provider}
                    disabled={saving || catalogLoading}
                    onChange={(event) => onProviderChange(event.target.value)}
                    value={form.provider}
                  >
                    <option value="">{text.selectProvider}</option>
                    {providers.map((provider) => {
                      const id = "slug" in provider ? provider.slug : provider.provider;
                      return <option key={id} value={id}>{provider.name}</option>;
                    })}
                  </select>
                </FormField>
                <FormField label={text.model}>
                  <select
                    aria-label={text.model}
                    disabled={saving || !form.provider}
                    onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))}
                    value={form.model}
                  >
                    <option value="">{text.selectModel}</option>
                    {models.map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}
                  </select>
                </FormField>
              </>
            )}
            {supportsGateway(form.kind) ? (
              <label className="gui-chat-model-checkbox">
                <input
                  checked={form.useGateway}
                  disabled={saving}
                  onChange={(event) => setForm((current) => ({ ...current, useGateway: event.target.checked }))}
                  type="checkbox"
                />
                <span>{text.useGateway}</span>
              </label>
            ) : null}
            {selectedMediaProvider && !selectedMediaProvider.available ? (
              <div className="gui-chat-model-form-note is-warning">{text.credentialMissingForProvider}</div>
            ) : null}
          </>
        ) : (
          <>
            {editing ? (
              <div className="gui-chat-model-form-note is-warning">
                {text.customWriteOnly}
              </div>
            ) : null}
            <FormField label={text.model}>
              <input aria-label={text.model} disabled={saving} onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))} placeholder="model-id" value={form.model} />
            </FormField>
            <FormField label={text.baseUrl}>
              <input aria-label={text.baseUrl} disabled={saving} onChange={(event) => setForm((current) => ({ ...current, baseUrl: event.target.value }))} placeholder="https://api.example.com/v1" value={form.baseUrl} />
            </FormField>
            <div className="gui-chat-skills-editor-grid">
              <FormField label={text.apiMode}>
                <input aria-label={text.apiMode} disabled={saving} onChange={(event) => setForm((current) => ({ ...current, apiMode: event.target.value }))} value={form.apiMode} />
              </FormField>
              <FormField label={text.contextLength}>
                <input aria-label={text.contextLength} disabled={saving} inputMode="numeric" min="1" onChange={(event) => setForm((current) => ({ ...current, contextLength: event.target.value }))} type="number" value={form.contextLength} />
              </FormField>
            </div>
            <FormField label={editing ? text.apiKeyKeep : text.apiKeyOptional}>
              <input aria-label={editing ? text.apiKeyKeep : text.apiKeyOptional} autoComplete="new-password" disabled={saving} onChange={(event) => setForm((current) => ({ ...current, apiKey: event.target.value }))} type="password" value={form.apiKey} />
            </FormField>
          </>
        )}

        <div className="gui-chat-workspace-dialog-actions">
          <button disabled={saving} onClick={onClose} type="button">{t.common.cancel}</button>
          <button className="is-primary" disabled={saving} type="submit">{saving ? text.saving : text.saveModel}</button>
        </div>
      </form>
    </GuiChatWorkspaceDialog>
  );
}

function FormField({ children, label }: { children: ReactNode; label: string }) {
  return <label className="gui-chat-model-field"><span>{label}</span>{children}</label>;
}

function validateForm(form: RegistrationFormState, text: ReturnType<typeof guiChatTranslations>["models"]): string | null {
  if (!form.name.trim()) return text.nameRequired;
  if (form.kind === "chat" && form.source === "custom") {
    if (!form.model.trim()) return text.modelRequired;
    if (!form.baseUrl.trim()) return text.baseUrlRequired;
    return null;
  }
  if (!form.provider) return text.providerRequired;
  if (!form.model) return text.modelRequired;
  return null;
}

function emptyActive(): ModelRegistrationsResponse["active"] {
  return {
    chat: { model: "", provider: "", registration_id: null },
    code: { model: "", provider: "", registration_id: null },
    image: { model: "", provider: "", registration_id: null },
    video: { model: "", provider: "", registration_id: null },
    voice: { model: "", provider: "", registration_id: null },
    vector: { model: "", provider: "", registration_id: null },
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
