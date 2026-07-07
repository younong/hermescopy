import { useState } from "react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { ChevronDown, ChevronRight, Wrench } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ImageArtifactState, ToolCallState } from "../types";
import { ImageArtifactCard } from "./ImageArtifactCard";

const STATUS_TONE = {
  failed: "destructive",
  running: "warning",
  succeeded: "success",
} as const;

const MAX_TOOL_DETAILS_CHARS = 120_000;

export function ToolCallCard({
  artifacts,
  tool,
}: {
  artifacts: ImageArtifactState[];
  tool: ToolCallState;
}) {
  const [open, setOpen] = useState(tool.status !== "succeeded");
  const details = tool.output || tool.error || tool.argsText || stringifyInput(tool.input);
  const displayedDetails = truncateForDisplay(details);
  const detailsTruncated = details.length > MAX_TOOL_DETAILS_CHARS;
  return (
    <section className="border border-current/15 bg-background-base/70">
      <button
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-midground/5"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        <Wrench className="h-4 w-4 text-warning" />
        <span className="min-w-0 flex-1 truncate font-mono-ui">{tool.name}</span>
        {tool.durationSeconds !== undefined ? (
          <span className="text-xs text-text-tertiary">{tool.durationSeconds.toFixed(1)}s</span>
        ) : null}
        <Badge tone={STATUS_TONE[tool.status]}>{tool.status}</Badge>
      </button>
      {open ? (
        <div className="space-y-2 border-t border-current/10 px-3 py-2">
          {tool.summary ? <p className="text-sm text-text-secondary">{tool.summary}</p> : null}
          {details ? (
            <>
              <pre
                className={cn(
                  "max-h-72 overflow-auto whitespace-pre-wrap break-words bg-secondary/50 px-3 py-2 text-xs leading-relaxed [overflow-wrap:anywhere]",
                  tool.error ? "text-destructive" : "text-text-secondary",
                )}
              >
                {displayedDetails}
              </pre>
              {detailsTruncated ? (
                <p className="text-xs text-text-tertiary">
                  Output truncated in UI after {MAX_TOOL_DETAILS_CHARS.toLocaleString()} characters.
                </p>
              ) : null}
            </>
          ) : (
            <p className="text-xs text-text-tertiary">No output yet.</p>
          )}
          {details ? (
            <Button
              ghost
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={() => void navigator.clipboard?.writeText(details)}
            >
              Copy output
            </Button>
          ) : null}
          {artifacts.map((artifact) => (
            <ImageArtifactCard artifact={artifact} key={artifact.id} />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function stringifyInput(input: unknown): string {
  if (input === undefined || input === null) return "";
  if (typeof input === "string") return input;
  try {
    return truncateForDisplay(JSON.stringify(input, null, 2));
  } catch {
    return truncateForDisplay(String(input));
  }
}

function truncateForDisplay(value: string): string {
  if (value.length <= MAX_TOOL_DETAILS_CHARS) return value;
  return `${value.slice(0, MAX_TOOL_DETAILS_CHARS)}\n\n[… truncated ${(
    value.length - MAX_TOOL_DETAILS_CHARS
  ).toLocaleString()} characters …]`;
}
