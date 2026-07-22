import { Button } from "@nous-research/ui/ui/components/button";
import { Autolinker } from "autolinker";
import { Check, CircleAlert, Copy } from "lucide-react";
import {
  Children,
  createContext,
  isValidElement,
  memo,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ComponentProps,
  type ElementType,
  type FC,
  type ReactNode,
} from "react";
import {
  defaultRemarkPlugins,
  Streamdown,
  type Components,
  type UrlTransform,
} from "streamdown";

import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

const TAG_CLASSES = {
  blockquote:
    "my-2 border-l-2 border-border/70 pl-3 italic text-text-secondary",
  del: "text-text-secondary line-through",
  em: "italic",
  h1: "mt-4 mb-2 text-base font-bold first:mt-0",
  h2: "mt-3 mb-2 text-sm font-bold first:mt-0",
  h3: "mt-3 mb-1.5 text-sm font-semibold first:mt-0",
  h4: "mt-2 mb-1 text-sm font-medium first:mt-0",
  hr: "my-3 border-border",
  li: "marker:text-text-tertiary",
  ol: "mb-2 list-decimal space-y-0.5 pl-5 last:mb-0",
  p: "mb-2 last:mb-0",
  pre: "overflow-x-auto border border-border bg-secondary/60 px-3 py-2.5 pr-11 font-mono text-xs leading-relaxed",
  strong: "font-semibold",
  td: "border-r border-border/50 px-3 py-2 align-top last:border-r-0",
  th: "border-r border-border/60 px-3 py-2 text-left font-semibold last:border-r-0",
  thead: "bg-secondary/50",
  ul: "mb-2 list-disc space-y-0.5 pl-5 last:mb-0",
} as const;

const HighlightTermsContext = createContext<readonly string[]>([]);

type MarkdownElementProps<T extends keyof React.JSX.IntrinsicElements> =
  ComponentProps<T> & { node?: unknown };

function tagged<T extends keyof typeof TAG_CLASSES>(Tag: T, highlight = false) {
  const Component = (({
    children,
    className,
    node,
    ...rest
  }: MarkdownElementProps<T>) => {
    const Element = Tag as ElementType;
    void node;

    return (
      <Element className={cn(TAG_CLASSES[Tag], className)} {...rest}>
        {highlight ? <HighlightedChildren>{children}</HighlightedChildren> : children}
      </Element>
    );
  }) as FC<MarkdownElementProps<T>>;

  Component.displayName = `Markdown.${Tag}`;
  return Component;
}

function MarkdownAnchor({
  children,
  className,
  href,
  node,
  ...rest
}: MarkdownElementProps<"a">) {
  void node;
  if (!href || !/^(https?:|mailto:)/i.test(href)) {
    return (
      <span className={className}>
        <HighlightedChildren>{children}</HighlightedChildren>
      </span>
    );
  }

  return (
    <a
      className={cn(
        "break-words text-primary underline decoration-primary/30 underline-offset-2 transition-colors [overflow-wrap:anywhere] hover:decoration-primary/60",
        className,
      )}
      href={href}
      rel="noreferrer"
      target="_blank"
      {...rest}
    >
      <HighlightedChildren>{children}</HighlightedChildren>
    </a>
  );
}

function MarkdownCode({ className, node, ...rest }: MarkdownElementProps<"code">) {
  void node;
  return (
    <code
      className={cn(
        "bg-secondary/60 px-1.5 py-0.5 font-mono text-xs text-primary/90",
        className,
      )}
      {...rest}
    />
  );
}

type CopyState = "copied" | "error";
type CopyFeedback = { state: CopyState; text: string } | null;

function codeBlockText(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) => {
      if (typeof child === "string" || typeof child === "number") return String(child);
      if (!isValidElement<{ children?: ReactNode }>(child)) return "";
      return codeBlockText(child.props.children);
    })
    .join("");
}

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through for HTTP/LAN dashboards where the modern API is unavailable.
  }

  const activeElement = document.activeElement;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();

  let copied = false;
  try {
    copied = document.execCommand?.("copy") ?? false;
  } catch {
    copied = false;
  } finally {
    textarea.remove();
    if (activeElement instanceof HTMLElement) activeElement.focus();
  }
  return copied;
}

function MarkdownPre({
  children,
  className,
  node,
  ...rest
}: MarkdownElementProps<"pre">) {
  const { t } = useI18n();
  const [copyFeedback, setCopyFeedback] = useState<CopyFeedback>(null);
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const text = codeBlockText(children);
  const copyState = copyFeedback?.text === text ? copyFeedback.state : "idle";
  void node;

  const clearResetTimer = useCallback(() => {
    if (resetTimer.current !== null) clearTimeout(resetTimer.current);
    resetTimer.current = null;
  }, []);

  useEffect(() => clearResetTimer, [clearResetTimer]);

  const handleCopy = useCallback(async () => {
    clearResetTimer();
    const copied = await copyText(text);
    setCopyFeedback({ state: copied ? "copied" : "error", text });
    resetTimer.current = setTimeout(() => {
      setCopyFeedback(null);
      resetTimer.current = null;
    }, 1800);
  }, [clearResetTimer, text]);

  const copyCode = t.common.copyCode ?? "Copy code";
  const copied = t.common.copied ?? "Copied";
  const copyFailed = t.common.copyFailed ?? "Copy failed";
  const label = copyState === "copied" ? copied : copyState === "error" ? copyFailed : copyCode;

  return (
    <div className="relative mb-2 last:mb-0" data-markdown-code-block>
      <pre className={cn(TAG_CLASSES.pre, className)} {...rest}>
        {children}
      </pre>
      <Button
        aria-label={label}
        className={cn(
          "absolute right-1.5 top-1.5 border border-border/70 bg-background/80 text-text-secondary backdrop-blur-sm hover:text-foreground",
          copyState === "copied" && "text-success",
          copyState === "error" && "text-destructive",
        )}
        ghost
        onClick={() => void handleCopy()}
        size="xs"
        title={label}
        type="button"
      >
        {copyState === "copied" ? (
          <Check aria-hidden />
        ) : copyState === "error" ? (
          <CircleAlert aria-hidden />
        ) : (
          <Copy aria-hidden />
        )}
      </Button>
      <span
        className="sr-only"
        role={copyState === "error" ? "alert" : "status"}
      >
        {copyState === "idle" ? "" : label}
      </span>
    </div>
  );
}

function MarkdownImage({ alt, className, node }: MarkdownElementProps<"img">) {
  void node;
  return alt ? <span className={className}>{alt}</span> : null;
}

function MarkdownTable({
  className,
  node,
  ...rest
}: MarkdownElementProps<"table">) {
  void node;
  return (
    <div
      className="mb-2 max-w-full overflow-x-auto border border-border last:mb-0"
      data-markdown-table-wrapper
    >
      <table
        className={cn(
          "w-full min-w-max border-collapse text-left text-xs [&_tr]:border-b [&_tr]:border-border/50 [&_tr:last-child]:border-b-0",
          className,
        )}
        {...rest}
      />
    </div>
  );
}

const COMPONENTS: Components = {
  a: MarkdownAnchor,
  blockquote: tagged("blockquote", true),
  code: MarkdownCode,
  del: tagged("del", true),
  em: tagged("em", true),
  h1: tagged("h1", true),
  h2: tagged("h2", true),
  h3: tagged("h3", true),
  h4: tagged("h4", true),
  hr: tagged("hr"),
  img: MarkdownImage,
  li: tagged("li", true),
  ol: tagged("ol"),
  p: tagged("p", true),
  pre: MarkdownPre,
  strong: tagged("strong", true),
  table: MarkdownTable,
  td: tagged("td", true),
  th: tagged("th", true),
  thead: tagged("thead"),
  ul: tagged("ul"),
};

const safeUrlTransform: UrlTransform = (url, key) => {
  if (key !== "href") return null;
  return /^(https?:|mailto:)/i.test(url.trim()) ? url : null;
};

const bareUrlParser = new Autolinker({
  email: false,
  phone: false,
  sanitizeHtml: false,
  stripPrefix: false,
});

function linkifyBareUrls() {
  return (tree: MarkdownAstNode) => {
    visitMarkdownText(tree);
  };
}

function visitMarkdownText(node: MarkdownAstNode) {
  if (!node.children || node.type === "code" || node.type === "inlineCode") return;

  const children: MarkdownAstNode[] = [];
  for (const child of node.children) {
    if (child.type === "text" && child.value) {
      children.push(...linkifyTextNode(child.value));
      continue;
    }

    const repaired = repairGfmBareUrl(child);
    if (repaired) {
      children.push(...repaired);
      continue;
    }

    visitMarkdownText(child);
    children.push(child);
  }
  node.children = children;
}

function repairGfmBareUrl(node: MarkdownAstNode): MarkdownAstNode[] | null {
  const text = node.children?.length === 1 ? node.children[0].value : undefined;
  if (node.type !== "link" || !text || node.url !== text) return null;

  const repaired = linkifyTextNode(text);
  return repaired.length === 1 && repaired[0].type === "link" ? null : repaired;
}

function linkifyTextNode(value: string): MarkdownAstNode[] {
  const matches = bareUrlParser.parse(value).filter((match) => match.getType() === "url");
  if (matches.length === 0) return [{ type: "text", value }];

  const nodes: MarkdownAstNode[] = [];
  let cursor = 0;
  for (const match of matches) {
    const start = match.getOffset();
    const matchedText = match.getMatchedText();
    if (start > cursor) nodes.push({ type: "text", value: value.slice(cursor, start) });
    nodes.push({
      children: [{ type: "text", value: matchedText }],
      type: "link",
      url: match.getAnchorHref(),
    });
    cursor = start + matchedText.length;
  }
  if (cursor < value.length) nodes.push({ type: "text", value: value.slice(cursor) });
  return nodes;
}

type MarkdownAstNode = {
  children?: MarkdownAstNode[];
  type: string;
  url?: string;
  value?: string;
};

const REMARK_PLUGINS = [
  linkifyBareUrls,
  ...Object.values(defaultRemarkPlugins),
];

export const Markdown = memo(function Markdown({
  content,
  highlightTerms,
  streaming,
}: {
  content: string;
  highlightTerms?: string[];
  streaming?: boolean;
}) {
  const terms = highlightTerms ?? [];

  return (
    <HighlightTermsContext.Provider value={terms}>
      <div
        className="min-w-0 break-words text-sm leading-relaxed text-foreground [overflow-wrap:anywhere]"
        data-markdown-streaming={streaming ? "true" : undefined}
      >
        <Streamdown
          caret="block"
          components={COMPONENTS}
          controls={false}
          isAnimating={Boolean(streaming)}
          lineNumbers={false}
          mode={streaming ? "streaming" : "static"}
          parseIncompleteMarkdown={Boolean(streaming)}
          remarkPlugins={REMARK_PLUGINS}
          skipHtml
          urlTransform={safeUrlTransform}
        >
          {content}
        </Streamdown>
      </div>
    </HighlightTermsContext.Provider>
  );
});

function HighlightedChildren({ children }: { children: ReactNode }) {
  const terms = useContext(HighlightTermsContext);

  return Children.map(children, (child) =>
    typeof child === "string" ? <HighlightedText text={child} terms={terms} /> : child,
  );
}

function HighlightedText({ text, terms }: { text: string; terms: readonly string[] }) {
  const escaped = terms
    .filter(Boolean)
    .map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (escaped.length === 0) return <>{text}</>;

  const regex = new RegExp(`(${escaped.join("|")})`, "gi");
  const exactMatch = new RegExp(`^(?:${escaped.join("|")})$`, "i");

  return (
    <>
      {text.split(regex).map((part, index) =>
        exactMatch.test(part) ? (
          <mark className="bg-warning/30 px-0.5 text-warning" key={index}>
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}
