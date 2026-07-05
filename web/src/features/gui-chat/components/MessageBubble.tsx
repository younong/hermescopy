import { Badge } from "@nous-research/ui/ui/components/badge";
import { Markdown } from "@/components/Markdown";
import { cn } from "@/lib/utils";
import type { ChatMessage, ImageArtifactState } from "../types";
import { ImageArtifactCard } from "./ImageArtifactCard";

const ROLE_LABEL: Record<ChatMessage["role"], string> = {
  assistant: "Assistant",
  system: "System",
  user: "User",
};

export function MessageBubble({
  artifacts,
  message,
}: {
  artifacts: ImageArtifactState[];
  message: ChatMessage;
}) {
  const isUser = message.role === "user";
  return (
    <article
      className={cn(
        "flex w-full",
        isUser ? "justify-end" : "justify-start",
      )}
    >
      <div
        className={cn(
          "max-w-[min(52rem,92%)] border px-4 py-3 shadow-sm",
          isUser
            ? "border-primary/25 bg-primary/10"
            : message.role === "system"
              ? "border-warning/25 bg-warning/10"
              : "border-current/15 bg-midground/5",
        )}
      >
        <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-[0.12em] text-text-tertiary">
          <span>{ROLE_LABEL[message.role]}</span>
          {message.streaming ? <Badge tone="warning">streaming</Badge> : null}
          {message.status === "error" ? <Badge tone="destructive">error</Badge> : null}
          {message.status === "interrupted" ? <Badge tone="secondary">stopped</Badge> : null}
        </div>
        {message.text ? (
          <Markdown content={message.text} streaming={message.streaming} />
        ) : message.streaming ? (
          <div className="text-sm text-text-secondary">Thinking…</div>
        ) : null}
        {artifacts.map((artifact) => (
          <ImageArtifactCard artifact={artifact} key={artifact.id} />
        ))}
      </div>
    </article>
  );
}
