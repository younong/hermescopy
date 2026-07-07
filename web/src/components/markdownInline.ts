export type InlineNode =
  | { type: "text"; content: string }
  | { type: "code"; content: string }
  | { type: "bold"; content: string }
  | { type: "italic"; content: string }
  | { type: "link"; text: string; href: string }
  | { type: "br" };

export function parseInline(text: string): InlineNode[] {
  const nodes: InlineNode[] = [];
  // Pattern priority: code > link > bold > italic > bare URL > line break
  const pattern =
    /(`[^`]+`)|(\[([^\]]+)\]\(([^)]+)\))|(\*\*([^*]+)\*\*)|(\*([^*]+)\*)|(\bhttps?:\/\/[^\s<>)\]]+)|(\n)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push({ type: "text", content: text.slice(lastIndex, match.index) });
    }

    if (match[1]) {
      // Inline code
      nodes.push({ type: "code", content: match[1].slice(1, -1) });
    } else if (match[2]) {
      // [text](url) link
      nodes.push({ type: "link", text: match[3], href: match[4] });
    } else if (match[5]) {
      // **bold**
      nodes.push({ type: "bold", content: match[6] });
    } else if (match[7]) {
      // *italic*
      nodes.push({ type: "italic", content: match[8] });
    } else if (match[9]) {
      // Bare URL
      const { href, trailingText } = trimAutolinkBoundary(match[9]);
      if (href) nodes.push({ type: "link", text: href, href });
      if (trailingText) nodes.push({ type: "text", content: trailingText });
    } else if (match[10]) {
      // Line break within paragraph
      nodes.push({ type: "br" });
    }

    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    nodes.push({ type: "text", content: text.slice(lastIndex) });
  }

  return nodes;
}

function trimAutolinkBoundary(value: string): { href: string; trailingText: string } {
  let href = value;
  let trailingText = "";
  const trailingPunctuationRe = /[.,;:!?。，、；：！？]+$/;
  while (href) {
    const punctuation = href.match(trailingPunctuationRe)?.[0];
    if (punctuation) {
      href = href.slice(0, -punctuation.length);
      trailingText = `${punctuation}${trailingText}`;
      continue;
    }

    const close = href.at(-1);
    if (!close) break;
    const open = CLOSING_DELIMITER_PAIRS[close];
    if (!open) break;
    if (countChar(href, close) <= countChar(href, open)) break;
    href = href.slice(0, -close.length);
    trailingText = `${close}${trailingText}`;
  }
  return { href, trailingText };
}

const CLOSING_DELIMITER_PAIRS: Record<string, string> = {
  ")": "(",
  "]": "[",
  "}": "{",
  ">": "<",
  "）": "（",
  "】": "【",
  "》": "《",
  "”": "“",
  "’": "‘",
  "」": "「",
  "』": "『",
};

function countChar(value: string, char: string): number {
  return Array.from(value).filter((candidate) => candidate === char).length;
}
