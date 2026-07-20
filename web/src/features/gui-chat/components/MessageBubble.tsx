import { Badge } from "@nous-research/ui/ui/components/badge";
import { useDeferredValue } from "react";

import { Markdown } from "@/components/Markdown";
import { cn } from "@/lib/utils";
import type { ArtifactState, ChatMessage } from "../types";
import { ArtifactCard } from "./ArtifactCard";
import { MessageAttachmentCard } from "./MessageAttachmentCard";

export function MessageBubble({
  artifacts,
  message,
}: {
  artifacts: ArtifactState[];
  message: ChatMessage;
}) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <article className="flex w-full min-w-0 justify-end">
        <div className="flex min-w-0 max-w-[min(34rem,92%)] flex-col items-end gap-3">
          {message.attachments?.length ? (
            <div className="flex flex-col items-end gap-3">
              {message.attachments.map((attachment) => (
                <MessageAttachmentCard
                  attachment={attachment}
                  key={attachment.id}
                  variant="bubble"
                />
              ))}
            </div>
          ) : null}

          {artifacts.length > 0 ? (
            <div className="flex flex-col items-end gap-3">
              {artifacts.map((artifact) => (
                <ArtifactCard artifact={artifact} key={artifact.id} variant="bubble" />
              ))}
            </div>
          ) : null}

          {message.text ? (
            <div className="min-w-0 max-w-full rounded-3xl bg-current/[0.06] px-5 py-3 text-base leading-relaxed break-words text-text-primary shadow-sm [overflow-wrap:anywhere]">
              <Markdown content={message.text} streaming={message.streaming} />
            </div>
          ) : null}
        </div>
      </article>
    );
  }

  return (
    <article className="flex w-full min-w-0 justify-start">
      <div
        className={cn(
          "min-w-0 max-w-[min(52rem,92%)] px-1 py-1",
          message.role === "system" ? "text-warning" : "text-text-primary",
        )}
      >
        <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-[0.12em] text-text-tertiary">
          {message.streaming ? <Badge tone="warning">streaming</Badge> : null}
          {message.status === "error" ? <Badge tone="destructive">error</Badge> : null}
          {message.status === "interrupted" ? <Badge tone="secondary">stopped</Badge> : null}
        </div>
        {message.text ? (
          <AssistantMarkdown text={message.text} streaming={message.streaming} />
        ) : message.streaming ? (
          <div className="text-sm text-text-secondary">Thinking…</div>
        ) : null}
        {message.attachments?.length ? (
          <div className="mt-3 flex flex-wrap gap-2">
            {message.attachments.map((attachment) => (
              <MessageAttachmentCard attachment={attachment} key={attachment.id} />
            ))}
          </div>
        ) : null}
        {artifacts.map((artifact) => (
          <ArtifactCard artifact={artifact} key={artifact.id} />
        ))}
      </div>
    </article>
  );
}

function AssistantMarkdown({ text, streaming }: { text: string; streaming?: boolean }) {
  const deferredText = useDeferredValue(text);

  return <Markdown content={streaming ? deferredText : text} streaming={streaming} />;
}
