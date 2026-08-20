import "./style.css";
type SessionInfo = {
  id: string;
  title?: string | null;
  source?: string | null;
  message_count: number;
  last_active: number;
};

type SessionStoreStats = {
  total: number;
  active_store: number;
  archived: number;
  messages: number;
  by_source: Record<string, number>;
};

type CompositionSegment = {
  id: string;
  label: string;
  value: number | null;
  percentage: number | null;
  unit: "messages" | "rough_tokens";
  status: "exact" | "estimated" | "unavailable";
};

type CompositionChart = {
  id: string;
  label: string;
  availability: "available" | "partial" | "unavailable";
  accuracy: "exact_count" | "rough_heuristic" | "unavailable";
  unit: "messages" | "rough_tokens";
  total: number | null;
  known_total: number;
  segments: CompositionSegment[];
  limitations: Array<{ code?: string; message?: string }>;
  coverage: {
    requested_sessions?: number;
    included_sessions?: number;
    available_sessions?: number;
  };
};

type CompositionResponse = {
  charts: CompositionChart[];
  coverage: CompositionChart["coverage"];
  limitations: Array<{ code?: string; message?: string }>;
};

type PluginI18n = {
  locale?: string;
};

const sdk = window.__HERMES_PLUGIN_SDK__;
const registry = window.__HERMES_PLUGINS__;

if (!sdk || !registry) {
  throw new Error("Hermes plugin SDK is unavailable");
}
const hostSdk = sdk;
const pluginRegistry = registry;

const React = hostSdk.React;
const { useCallback, useEffect, useMemo, useState } = hostSdk.hooks;
const { Badge, Button, Checkbox } = hostSdk.components as Record<string, any>;
const api = hostSdk.api as {
  getSessionComposition(ids: string[], options?: { signal?: AbortSignal }): Promise<CompositionResponse>;
  getSessions(
    limit?: number,
    offset?: number,
    order?: "created" | "recent",
    compact?: boolean,
    options?: { active_from?: number; active_before?: number; signal?: AbortSignal },
  ): Promise<{ sessions: SessionInfo[]; total: number }>;
  getSessionStats(): Promise<SessionStoreStats>;
};

const COPY = {
  en: {
    title: "Message statistics",
    subtitle: "A bounded view of recent conversations and their message composition.",
    total: "Total sessions",
    active: "Active in store",
    archived: "Archived",
    messages: "Messages",
    sources: "Sources",
    recent: "Recent sessions",
    recentHelp: "Select up to 50 of the 50 most recently active sessions.",
    selectAll: "Select all",
    clear: "Clear",
    selected: "{count} selected",
    composition: "Message composition",
    loading: "Loading message statistics…",
    loadingComposition: "Loading composition…",
    loadError: "Message statistics could not be loaded.",
    compositionError: "Session composition could not be loaded.",
    retry: "Retry",
    empty: "No sessions are available yet.",
    emptyComposition: "Select at least one recent session to see its composition.",
    unavailable: "Unavailable",
    partial: "Partial",
    exact: "Exact",
    estimated: "Estimated",
    coverage: "Coverage",
    limitations: "Limitations",
    allSources: "No source data",
    untitled: "Untitled session",
    messageUnit: "messages",
    tokenUnit: "rough tokens",
  },
  zh: {
    title: "消息统计",
    subtitle: "查看近期对话的消息总量、来源与构成，最多分析 50 个会话。",
    total: "会话总数",
    active: "存储中活跃",
    archived: "已归档",
    messages: "消息",
    sources: "来源",
    recent: "近期会话",
    recentHelp: "可从最近活跃的 50 个会话中选择最多 50 个。",
    selectAll: "全选",
    clear: "清除",
    selected: "已选择 {count} 个",
    composition: "消息构成",
    loading: "正在加载消息统计…",
    loadingComposition: "正在加载构成…",
    loadError: "无法加载消息统计。",
    compositionError: "无法加载会话构成。",
    retry: "重试",
    empty: "暂无可用会话。",
    emptyComposition: "请至少选择一个近期会话以查看构成。",
    unavailable: "不可用",
    partial: "部分数据",
    exact: "精确",
    estimated: "估算",
    coverage: "覆盖范围",
    limitations: "限制",
    allSources: "暂无来源数据",
    untitled: "无标题会话",
    messageUnit: "条消息",
    tokenUnit: "粗略 Token",
  },
} as const;

const COLORS = ["#3867ed", "#2b8a66", "#d18400", "#7b61c9", "#cf596d", "#3791a6"];
const CIRCUMFERENCE = 2 * Math.PI * 42;
const RECENT_LIMIT = 50;
const SELECTION_LIMIT = 50;
const NUMBER_FORMATTER = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

function colorFor(id: string) {
  let hash = 0;
  for (const character of id) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return COLORS[hash % COLORS.length];
}

function formatNumber(value: number) {
  return NUMBER_FORMATTER.format(value);
}

function percentage(segment: CompositionSegment, knownTotal: number) {
  if (segment.percentage !== null) return segment.percentage;
  if (segment.value === null || knownTotal <= 0) return null;
  return (segment.value / knownTotal) * 100;
}

function limitationText(limitation: { code?: string; message?: string }) {
  return limitation.message || limitation.code?.replaceAll("_", " ") || "";
}

function MessageStatisticsWorkspace() {
  const i18n = hostSdk.useI18n() as PluginI18n;
  const copy = i18n.locale?.toLowerCase().startsWith("zh") ? COPY.zh : COPY.en;
  const [stats, setStats] = useState<SessionStoreStats | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [composition, setComposition] = useState<CompositionResponse | null>(null);
  const [compositionKey, setCompositionKey] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [compositionLoading, setCompositionLoading] = useState(false);
  const [compositionError, setCompositionError] = useState(false);
  const [retry, setRetry] = useState(0);
  const [compositionRetry, setCompositionRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(false);
    Promise.all([
      api.getSessionStats(),
      api.getSessions(RECENT_LIMIT, 0, "recent", true, { signal: controller.signal }),
    ])
      .then(([nextStats, response]) => {
        if (controller.signal.aborted) return;
        setStats(nextStats);
        setSessions(response.sessions);
        setSelected((current) => {
          const available = new Set(response.sessions.map((session) => session.id));
          return new Set(Array.from(current).filter((id) => available.has(id)));
        });
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoadError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [retry]);

  const selectedIds = useMemo(() => Array.from(selected).sort(), [selected]);
  const selectionKey = JSON.stringify(selectedIds);

  useEffect(() => {
    if (selectedIds.length === 0) {
      setComposition(null);
      setCompositionKey("");
      setCompositionLoading(false);
      setCompositionError(false);
      return;
    }
    const controller = new AbortController();
    setComposition(null);
    setCompositionKey("");
    setCompositionLoading(true);
    setCompositionError(false);
    api.getSessionComposition(selectedIds, { signal: controller.signal })
      .then((response) => {
        if (controller.signal.aborted) return;
        setComposition(response);
        setCompositionKey(selectionKey);
      })
      .catch(() => {
        if (!controller.signal.aborted) setCompositionError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setCompositionLoading(false);
      });
    return () => controller.abort();
  }, [selectionKey, compositionRetry]);

  const toggleSession = useCallback((id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else if (next.size < SELECTION_LIMIT) next.add(id);
      return next;
    });
  }, []);

  if (loading && !stats) {
    return <div className="message-statistics-feedback" role="status">{copy.loading}</div>;
  }

  if (loadError && !stats) {
    return (
      <div className="message-statistics-feedback is-error" role="alert">
        <span>{copy.loadError}</span>
        <Button outlined size="sm" onClick={() => setRetry((value: number) => value + 1)}>{copy.retry}</Button>
      </div>
    );
  }

  return (
    <div className="message-statistics-pane">
      <header className="message-statistics-header">
        <div>
          <h2>{copy.title}</h2>
          <p>{copy.subtitle}</p>
        </div>
        {loadError && <Button outlined size="sm" onClick={() => setRetry((value: number) => value + 1)}>{copy.retry}</Button>}
      </header>

      {stats && (
        <section className="message-statistics-summary" aria-label={copy.title}>
          <SummaryValue label={copy.total} value={stats.total} />
          <SummaryValue label={copy.active} value={stats.active_store} tone="success" />
          <SummaryValue label={copy.archived} value={stats.archived} />
          <SummaryValue label={copy.messages} value={stats.messages} />
        </section>
      )}

      <section className="message-statistics-section">
        <div className="message-statistics-section-heading">
          <div>
            <h3>{copy.sources}</h3>
            <p>{stats && Object.keys(stats.by_source).length === 0 ? copy.allSources : ""}</p>
          </div>
        </div>
        {stats && Object.keys(stats.by_source).length > 0 && (
          <div className="message-statistics-sources">
            {Object.entries(stats.by_source)
              .sort(([, left], [, right]) => right - left)
              .map(([source, count]) => <Badge key={source} tone="outline">{source} · {count}</Badge>)}
          </div>
        )}
      </section>

      <section className="message-statistics-section">
        <div className="message-statistics-section-heading">
          <div>
            <h3>{copy.recent}</h3>
            <p>{copy.recentHelp}</p>
          </div>
          <div className="message-statistics-actions">
            <span>{copy.selected.replace("{count}", String(selected.size))}</span>
            <Button ghost size="sm" onClick={() => setSelected(new Set(sessions.slice(0, SELECTION_LIMIT).map((session) => session.id)))}>{copy.selectAll}</Button>
            <Button ghost size="sm" onClick={() => setSelected(new Set())}>{copy.clear}</Button>
          </div>
        </div>
        {sessions.length === 0 ? (
          <div className="message-statistics-empty">{copy.empty}</div>
        ) : (
          <div className="message-statistics-session-list">
            {sessions.map((session) => (
              <label className="message-statistics-session" key={session.id}>
                <Checkbox
                  aria-label={session.title || copy.untitled}
                  checked={selected.has(session.id)}
                  onClick={() => toggleSession(session.id)}
                />
                <span className="message-statistics-session-copy">
                  <strong>{session.title || copy.untitled}</strong>
                  <small>{session.source || "local"} · {session.message_count} {copy.messageUnit} · {hostSdk.utils.timeAgo(session.last_active)}</small>
                </span>
              </label>
            ))}
          </div>
        )}
      </section>

      <section className="message-statistics-section" aria-label={copy.composition}>
        <div className="message-statistics-section-heading">
          <div><h3>{copy.composition}</h3></div>
        </div>
        {selected.size === 0 && <div className="message-statistics-empty">{copy.emptyComposition}</div>}
        {compositionLoading && <div className="message-statistics-feedback" role="status">{copy.loadingComposition}</div>}
        {compositionError && (
          <div className="message-statistics-feedback is-error" role="alert">
            <span>{copy.compositionError}</span>
            <Button outlined size="sm" onClick={() => setCompositionRetry((value: number) => value + 1)}>{copy.retry}</Button>
          </div>
        )}
        {!compositionError && compositionKey === selectionKey && composition && composition.charts.length === 0 && selected.size > 0 && (
          <div className="message-statistics-empty">{copy.unavailable}</div>
        )}
        {!compositionError && compositionKey === selectionKey && composition && composition.charts.length > 0 && (
          <div className="message-statistics-charts">
            {composition.charts.map((chart) => <CompositionCard chart={chart} copy={copy} key={chart.id} />)}
          </div>
        )}
      </section>
    </div>
  );
}

function SummaryValue({ label, tone, value }: { label: string; tone?: "success"; value: number }) {
  return (
    <div className="message-statistics-summary-value">
      <strong className={tone === "success" ? "is-success" : undefined}>{formatNumber(value)}</strong>
      <span>{label}</span>
    </div>
  );
}

function CompositionCard({ chart, copy }: { chart: CompositionChart; copy: typeof COPY.en | typeof COPY.zh }) {
  const drawable = chart.segments.filter((segment) => segment.status !== "unavailable" && (segment.value ?? 0) > 0);
  const total = drawable.reduce((sum, segment) => sum + (segment.value ?? 0), 0);
  let offset = 0;
  const coverageIncluded = chart.coverage.included_sessions ?? chart.coverage.available_sessions;
  const limitations = chart.limitations.map(limitationText).filter(Boolean);

  return (
    <article className="message-statistics-chart">
      <div className="message-statistics-chart-heading">
        <h4>{chart.label}</h4>
        <Badge tone={chart.availability === "available" ? "success" : chart.availability === "partial" ? "warning" : "outline"}>
          {chart.availability === "unavailable" ? copy.unavailable : chart.availability === "partial" ? copy.partial : chart.accuracy === "exact_count" ? copy.exact : copy.estimated}
        </Badge>
      </div>
      {chart.availability === "unavailable" ? (
        <div className="message-statistics-empty">{copy.unavailable}</div>
      ) : (
        <div className="message-statistics-donut">
          <svg viewBox="0 0 100 100" role="img" aria-label={chart.label}>
            <circle cx="50" cy="50" r="42" fill="none" stroke="#e8ebef" strokeWidth="12" />
            {drawable.map((segment) => {
              const length = total > 0 ? ((segment.value ?? 0) / total) * CIRCUMFERENCE : 0;
              const start = offset;
              offset += length;
              return <circle key={segment.id} cx="50" cy="50" r="42" fill="none" stroke={colorFor(segment.id)} strokeWidth="12" strokeDasharray={`${length} ${CIRCUMFERENCE - length}`} strokeDashoffset={-start} transform="rotate(-90 50 50)" />;
            })}
            <text x="50" y="48" textAnchor="middle">{copy.messages}</text>
            <text x="50" y="60" textAnchor="middle">{formatNumber(chart.total ?? chart.known_total)}</text>
          </svg>
        </div>
      )}
      <ul className="message-statistics-legend">
        {chart.segments.map((segment) => {
          const percent = percentage(segment, chart.known_total);
          const unit = segment.unit === "messages" ? copy.messageUnit : copy.tokenUnit;
          return (
            <li key={segment.id}>
              <span className="message-statistics-dot" style={{ backgroundColor: colorFor(segment.id) }} />
              <span>{segment.label}</span>
              <strong>{segment.value === null ? copy.unavailable : `${formatNumber(segment.value)} ${unit}`}</strong>
              <small>{percent === null ? "—" : `${formatNumber(percent)}%`}</small>
              <Badge tone={segment.status === "exact" ? "success" : segment.status === "estimated" ? "warning" : "outline"}>
                {segment.status === "exact" ? copy.exact : segment.status === "estimated" ? copy.estimated : copy.unavailable}
              </Badge>
            </li>
          );
        })}
      </ul>
      {coverageIncluded !== undefined && chart.coverage.requested_sessions !== undefined && (
        <p className="message-statistics-note">{copy.coverage}: {coverageIncluded}/{chart.coverage.requested_sessions}</p>
      )}
      {limitations.length > 0 && (
        <div className="message-statistics-note"><strong>{copy.limitations}</strong><ul>{limitations.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul></div>
      )}
    </article>
  );
}

pluginRegistry.registerWorkspace("message-statistics", "statistics", MessageStatisticsWorkspace);

export { MessageStatisticsWorkspace };
