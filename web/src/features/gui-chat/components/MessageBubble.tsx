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
            <div data-message-variant="user" className="min-w-0 max-w-full rounded-[1.25rem] bg-[#f0f1f2] px-4 py-2.5 text-[0.9375rem] leading-6 break-words text-[#25282d] [overflow-wrap:anywhere]">
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
          "min-w-0 w-full px-1 py-1 text-[0.9375rem] leading-7",
          message.role === "system" ? "text-[#9a6700]" : "text-[#282b30]",
        )}
      >
        {message.text ? (
          <AssistantMarkdown text={message.text} streaming={message.streaming} />
        ) : message.streaming ? (
          <div className="text-sm text-[#8a8f97]">Thinking…</div>
        ) : null}
        {message.streaming || message.status === "error" || message.status === "interrupted" ? (
          <div className={cn("mt-3 text-xs text-[#92969d]", message.status === "error" && "text-[#b42318]")}>
            {message.streaming ? "Writing…" : message.status === "error" ? "Response failed" : "Stopped"}
          </div>
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
