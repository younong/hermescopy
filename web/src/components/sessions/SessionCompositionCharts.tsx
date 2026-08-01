import { useEffect, useMemo, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import type {
  SessionCompositionChart,
  SessionCompositionLimitation,
  SessionCompositionResponse,
  SessionCompositionSegment,
} from "@/lib/api";
import { sessionCompositionTranslations, useI18n } from "@/i18n";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

const COLORS = [
  "var(--composition-series-1, oklch(0.65 0.18 250))",
  "var(--composition-series-2, oklch(0.68 0.17 155))",
  "var(--composition-series-3, oklch(0.72 0.16 75))",
  "var(--composition-series-4, oklch(0.65 0.18 315))",
  "var(--composition-series-5, oklch(0.68 0.15 25))",
  "var(--composition-series-6, oklch(0.62 0.12 205))",
] as const;
const CIRCUMFERENCE = 2 * Math.PI * 42;

function colorFor(id: string): string {
  let hash = 0;
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return COLORS[hash % COLORS.length];
}

function limitationText(limitation: SessionCompositionLimitation): string {
  if (typeof limitation.message === "string" && limitation.message) {
    return limitation.message;
  }
  if (typeof limitation.code === "string" && limitation.code) {
    return limitation.code.replaceAll("_", " ");
  }
  try {
    return JSON.stringify(limitation);
  } catch {
    return String(limitation);
  }
}

const numberFormatter = new Intl.NumberFormat(undefined, {
  maximumFractionDigits: 1,
});

function formatNumber(value: number): string {
  return numberFormatter.format(value);
}

function segmentPercent(
  segment: SessionCompositionSegment,
  knownTotal: number,
): number | null {
  if (segment.percentage !== null) return segment.percentage;
  if (segment.value === null || knownTotal <= 0) return null;
  return (segment.value / knownTotal) * 100;
}

function coverageText(coverage: SessionCompositionChart["coverage"]): string | null {
  const included = coverage.included_sessions ?? coverage.available_sessions;
  const requested = coverage.requested_sessions;
  if (typeof included === "number" && typeof requested === "number") {
    return `${included}/${requested}`;
  }
  return null;
}

function CompositionCard({ chart }: { chart: SessionCompositionChart }) {
  const { t } = useI18n();
  const text = sessionCompositionTranslations(t);
  const drawable = chart.segments.filter(
    (segment) =>
      segment.status !== "unavailable" &&
      segment.value !== null &&
      segment.value > 0,
  );
  const drawableTotal = drawable.reduce(
    (sum, segment) => sum + (segment.value ?? 0),
    0,
  );
  const arcOffsets = drawable.reduce<number[]>((offsets, segment) => {
    const previous = offsets.at(-1) ?? 0;
    const length =
      drawableTotal > 0
        ? ((segment.value ?? 0) / drawableTotal) * CIRCUMFERENCE
        : 0;
    offsets.push(previous + length);
    return offsets;
  }, []);
  const unit = chart.unit === "messages" ? text.messages : text.roughTokens;
  const summary = chart.segments
    .map((segment) => {
      const value =
        segment.value === null ? text.unavailable : formatNumber(segment.value);
      const percent = segmentPercent(segment, chart.known_total);
      return `${segment.label}: ${value} ${unit}${
        percent === null ? "" : `, ${formatNumber(percent)}%`
      }`;
    })
    .join("; ");
  const limitations = chart.limitations.map(limitationText).filter(Boolean);
  const coverage = coverageText(chart.coverage);

  return (
    <Card className="min-w-0">
      <CardHeader className="gap-2">
        <div className="flex min-w-0 items-center justify-between gap-2">
          <CardTitle className="min-w-0 text-base">{chart.label}</CardTitle>
          <Badge
            tone={
              chart.availability === "available"
                ? "success"
                : chart.availability === "partial"
                  ? "warning"
                  : "outline"
            }
            className="shrink-0 text-xs"
          >
            {chart.availability === "partial"
              ? text.partial
              : chart.availability === "unavailable"
                ? text.unavailable
                : chart.accuracy === "exact_count"
                  ? text.exact
                  : text.estimated}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {chart.availability === "unavailable" ? (
          <div className="flex min-h-36 items-center justify-center text-sm text-muted-foreground">
            {text.unavailable}
          </div>
        ) : (
          <div className="flex justify-center">
            <svg
              viewBox="0 0 100 100"
              className="h-40 w-40"
              role="img"
              aria-label={`${chart.label}. ${summary}`}
            >
              <circle
                cx="50"
                cy="50"
                r="42"
                fill="none"
                stroke="currentColor"
                strokeWidth="12"
                className="text-muted/60"
              />
              {drawable.map((segment, index) => {
                const start = arcOffsets[index - 1] ?? 0;
                const length = arcOffsets[index] - start;
                return (
                  <circle
                    key={segment.id}
                    cx="50"
                    cy="50"
                    r="42"
                    fill="none"
                    stroke={colorFor(segment.id)}
                    strokeWidth="12"
                    strokeDasharray={`${length} ${CIRCUMFERENCE - length}`}
                    strokeDashoffset={-start}
                    transform="rotate(-90 50 50)"
                  />
                );
              })}
              <text
                x="50"
                y="47"
                textAnchor="middle"
                className="fill-foreground text-[9px] font-semibold"
              >
                {chart.total === null ? text.knownTotal : text.total}
              </text>
              <text
                x="50"
                y="59"
                textAnchor="middle"
                className="fill-muted-foreground text-[8px]"
              >
                {formatNumber(chart.total ?? chart.known_total)}
              </text>
            </svg>
          </div>
        )}

        <ul
          className="flex flex-col gap-2"
          aria-label={`${chart.label} ${text.legend}`}
        >
          {chart.segments.map((segment) => {
            const percent = segmentPercent(segment, chart.known_total);
            return (
              <li
                key={segment.id}
                className="flex min-w-0 items-start gap-2 text-xs"
              >
                <span
                  className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: colorFor(segment.id) }}
                />
                <span className="min-w-0 flex-1 break-words">
                  {segment.label}
                </span>
                <span className="shrink-0 text-right tabular-nums">
                  <span className="block font-medium">
                    {segment.value === null
                      ? text.unavailable
                      : formatNumber(segment.value)}{" "}
                    {segment.value === null ? "" : unit}
                  </span>
                  <span className="text-muted-foreground">
                    {percent === null
                      ? "—"
                      : text.percentage.replace(
                          "{value}",
                          formatNumber(percent),
                        )}
                  </span>
                </span>
                <Badge
                  tone={
                    segment.status === "unavailable"
                      ? "outline"
                      : segment.status === "exact"
                        ? "success"
                        : "warning"
                  }
                  className="shrink-0 text-[10px]"
                >
                  {segment.status === "unavailable"
                    ? text.unavailable
                    : segment.status === "exact"
                      ? text.exact
                      : text.estimated}
                </Badge>
              </li>
            );
          })}
        </ul>

        {chart.availability === "partial" && (
          <p className="text-xs text-warning">{text.partialKnownTotal}</p>
        )}
        {coverage && (
          <p className="text-xs text-muted-foreground">
            {text.coverage}: {coverage}
          </p>
        )}
        {limitations.length > 0 && (
          <div className="text-xs text-muted-foreground">
            <p className="font-medium text-foreground">{text.limitations}</p>
            <ul className="list-disc space-y-1 pl-4">
              {limitations.map((limitation, index) => (
                <li key={`${limitation}-${index}`}>{limitation}</li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function SessionCompositionCharts({
  ids,
  title,
}: {
  ids: string[];
  title?: string;
}) {
  const { t } = useI18n();
  const text = sessionCompositionTranslations(t);
  const requestKey = useMemo(
    () => JSON.stringify(Array.from(new Set(ids)).sort()),
    [ids],
  );
  const stableIds = useMemo<string[]>(() => JSON.parse(requestKey), [requestKey]);
  const [response, setResponse] = useState<SessionCompositionResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (stableIds.length === 0) {
      setResponse(null);
      setLoading(false);
      setError(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(false);
    api
      .getSessionComposition(stableIds, { signal: controller.signal })
      .then((next) => {
        if (!controller.signal.aborted) setResponse(next);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [requestKey, retry]);

  if (stableIds.length === 0) return null;

  return (
    <section
      className="flex min-w-0 flex-col gap-3"
      aria-label={title ?? text.title}
    >
      {title && (
        <h3 className="font-mondwest normal-case text-sm font-semibold">
          {title}
        </h3>
      )}
      {loading && !response && (
        <div
          className="flex items-center justify-center gap-2 border border-border py-8 text-sm text-muted-foreground"
          role="status"
        >
          <Spinner className="text-primary" /> {text.loading}
        </div>
      )}
      {error && (
        <div
          className="flex items-center justify-between gap-3 border border-destructive/30 bg-destructive/[0.04] p-3"
          role="alert"
        >
          <span className="flex items-center gap-2 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4" />
            {text.loadError}
          </span>
          <Button
            outlined
            size="sm"
            onClick={() => setRetry((value) => value + 1)}
          >
            {text.retry}
          </Button>
        </div>
      )}
      {!error && response && response.charts.length === 0 && (
        <p className="border border-border p-4 text-sm text-muted-foreground">
          {text.unavailable}
        </p>
      )}
      {!error && response && response.charts.length > 0 && (
        <div className="grid min-w-0 gap-3 xl:grid-cols-3">
          {response.charts.map((chart) => (
            <CompositionCard key={chart.id} chart={chart} />
          ))}
        </div>
      )}
    </section>
  );
}
