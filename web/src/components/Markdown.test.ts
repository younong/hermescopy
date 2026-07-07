import { describe, expect, it } from "vitest";

import { parseInline } from "./markdownInline";

const WECHAT_PROMPT =
  "分析咿呀咿呀哟喂公众号的两篇文章的阅读量差异化，链接：https://mp.weixin.qq.com/s/Dl28D1x2ti1ZfqIBD_axYw https://mp.weixin.qq.com/s/ZglvujhgYZ7ggnPTlubaBA";

describe("parseInline", () => {
  it("autolinks both WeChat article URLs in a Chinese prompt", () => {
    const links = parseInline(WECHAT_PROMPT).filter((node) => node.type === "link");

    expect(links).toEqual([
      {
        href: "https://mp.weixin.qq.com/s/Dl28D1x2ti1ZfqIBD_axYw",
        text: "https://mp.weixin.qq.com/s/Dl28D1x2ti1ZfqIBD_axYw",
        type: "link",
      },
      {
        href: "https://mp.weixin.qq.com/s/ZglvujhgYZ7ggnPTlubaBA",
        text: "https://mp.weixin.qq.com/s/ZglvujhgYZ7ggnPTlubaBA",
        type: "link",
      },
    ]);
  });

  it("keeps trailing Chinese punctuation out of bare URL hrefs", () => {
    expect(parseInline("参考：https://example.com/a?x=1。")).toEqual([
      { content: "参考：", type: "text" },
      { href: "https://example.com/a?x=1", text: "https://example.com/a?x=1", type: "link" },
      { content: "。", type: "text" },
    ]);
  });

  it("keeps unbalanced closing delimiters out of bare URL hrefs", () => {
    expect(parseInline("（https://example.com/a）")).toEqual([
      { content: "（", type: "text" },
      { href: "https://example.com/a", text: "https://example.com/a", type: "link" },
      { content: "）", type: "text" },
    ]);
  });
});
