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

export function ToolCallCard({
  artifacts,
  tool,
}: {
  artifacts: ImageArtifactState[];
  tool: ToolCallState;
}) {
  const [open, setOpen] = useState(tool.status !== "succeeded");
  const details = tool.output || tool.error || tool.argsText || stringifyInput(tool.input);
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
            <pre
              className={cn(
                "max-h-72 overflow-auto whitespace-pre-wrap bg-secondary/50 px-3 py-2 text-xs leading-relaxed",
                tool.error ? "text-destructive" : "text-text-secondary",
              )}
            >
              {details}
            </pre>
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
    return JSON.stringify(input, null, 2);
  } catch {
    return String(input);
  }
}
