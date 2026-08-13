import { CheckCircle2, ExternalLink, UsersRound } from "lucide-react";
import { useDeferredValue, useMemo } from "react";
import { Link } from "react-router-dom";

import { Markdown, type MarkdownFileLink } from "@/components/Markdown";
import { guiChatTranslations, useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { normalizeSessionFileReference } from "../files";
import type { ArtifactState, ChatMessage, MessageAttachmentState } from "../types";
import { ArtifactCard } from "./ArtifactCard";
import { InlineFileLink } from "./InlineFileLink";
import { MessageAttachmentCard } from "./MessageAttachmentCard";

export function MessageBubble({
  artifacts,
  message,
  onUseAttachmentAgain,
}: {
  artifacts: ArtifactState[];
  message: ChatMessage;
  onUseAttachmentAgain?: (attachment: MessageAttachmentState) => void;
}) {
  const { t } = useI18n();
  const copy = guiChatTranslations(t).messages;
  const isUser = message.role === "user";

  if (message.collaborationCard) {
    const card = message.collaborationCard;
    const completed = card.status === "completed";
    const Icon = completed ? CheckCircle2 : UsersRound;
    return (
      <article className="flex w-full min-w-0 justify-start">
        <div className="w-full rounded-2xl border border-[#dce4f7] bg-[#f7f9ff] p-4 text-[#283f79]">
          <div className="flex items-start gap-3">
            <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-[#e6edff] text-[#3867ed]">
              <Icon className="h-4 w-4" />
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#6f7fa8]">
                {copy.collaborationStatus.replace("{status}", card.status)}
              </p>
              <h3 className="mt-1 text-sm font-semibold text-[#252f4a]">{card.title}</h3>
              {card.text ? <div className="mt-2 text-xs leading-5 text-[#53617f]"><Markdown content={card.text} /></div> : null}
              <Link className="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-[#3867ed] hover:text-[#2852c7]" to={`?group=${encodeURIComponent(card.groupId)}`}>
                {copy.openGroup} <ExternalLink className="h-3 w-3" />
              </Link>
            </div>
          </div>
        </div>
      </article>
    );
  }

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
                  onUseAgain={onUseAttachmentAgain}
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
            <div data-message-variant="user" className="min-w-0 max-w-full rounded-[1.25rem] bg-[#f0f1f2] px-4 py-2.5 break-words text-[#25282d] [overflow-wrap:anywhere]">
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
        data-message-variant="assistant"
        className={cn(
          "min-w-0 w-full px-1 py-1",
          message.role === "system" ? "text-[#9a6700]" : "text-[#282b30]",
        )}
      >
        {message.text ? (
          <AssistantMarkdown
            artifacts={artifacts}
            text={message.text}
            streaming={message.streaming}
          />
        ) : message.streaming ? (
          <div className="text-[#8a8f97]">{copy.thinking}</div>
        ) : null}
        {message.streaming || message.status === "error" || message.status === "interrupted" ? (
          <div className={cn("mt-3 text-xs text-[#92969d]", message.status === "error" && "text-[#b42318]")}>
            {message.streaming ? copy.writing : message.status === "error" ? copy.responseFailed : copy.stopped}
          </div>
        ) : null}
        {message.attachments?.length ? (
          <div className="mt-3 flex flex-wrap gap-2">
            {message.attachments.map((attachment) => (
              <MessageAttachmentCard
                attachment={attachment}
                key={attachment.id}
                onUseAgain={onUseAttachmentAgain}
              />
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

function AssistantMarkdown({
  artifacts,
  text,
  streaming,
}: {
  artifacts: ArtifactState[];
  text: string;
  streaming?: boolean;
}) {
  const deferredText = useDeferredValue(text);
  const fileLinks = useMemo(
    () => artifacts.flatMap((artifact): MarkdownFileLink[] => {
      if (artifact.kind !== "file") return [];
      const sourcePath = normalizeSessionFileReference(artifact.sourcePath);
      if (!sourcePath) return [];
      return [{
        matches: (href) => normalizeSessionFileReference(href) === sourcePath,
        render: (children) => (
          <InlineFileLink artifact={artifact}>{children}</InlineFileLink>
        ),
      }];
    }),
    [artifacts],
  );

  return (
    <Markdown
      content={streaming ? deferredText : text}
      fileLinks={fileLinks}
      streaming={streaming}
    />
  );
}
