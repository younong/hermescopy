// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { Markdown } from "./Markdown";

const TABLE_MARKDOWN = `| 平台 | 记录的内容方向 |
|---|---|
| 微信视频号 | 电影分段剪辑；搞笑；教育类专家内容 |
| 抖音 | 短视频带货；中视频；小程序推广 |
| B站 | 科技类 UP 主 |`;

const WECHAT_PROMPT =
  "参考：https://mp.weixin.qq.com/s/Dl28D1x2ti1ZfqIBD_axYw。另见（https://example.com/a）。";

let root: Root | undefined;

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
});

async function renderMarkdown(
  content: string,
  props: { highlightTerms?: string[]; streaming?: boolean } = {},
) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  await act(async () => {
    root?.render(<Markdown content={content} {...props} />);
  });

  return container;
}

describe("Markdown", () => {
  it("renders a production-shaped Chinese GFM table as semantic markup", async () => {
    const container = await renderMarkdown(TABLE_MARKDOWN);
    const table = container.querySelector("table");
    const wrapper = container.querySelector("[data-markdown-table-wrapper]");

    expect(table).not.toBeNull();
    expect(wrapper?.contains(table)).toBe(true);
    expect(wrapper?.className).toContain("overflow-x-auto");
    expect(container.querySelectorAll("thead th")).toHaveLength(2);
    expect(container.querySelectorAll("tbody tr")).toHaveLength(3);
    expect(container.querySelector("thead")?.textContent).toContain("记录的内容方向");
    expect(container.querySelector("tbody")?.textContent).toContain("科技类 UP 主");
    expect(container.textContent).not.toContain("|---|---|");
  });

  it("preserves common assistant Markdown and bare URL boundaries", async () => {
    const container = await renderMarkdown(`## 标题

**粗体**、*斜体*、~~删除~~ 与 \`inline\`。

- 第一项
- 第二项

---

${WECHAT_PROMPT}

\`\`\`ts
const complete = true;
\`\`\``);
    const links = Array.from(container.querySelectorAll("a"));

    expect(container.querySelector("h2")?.textContent).toBe("标题");
    expect(container.querySelector("strong")?.textContent).toBe("粗体");
    expect(container.querySelector("em")?.textContent).toBe("斜体");
    expect(container.querySelector("del")?.textContent).toBe("删除");
    expect(container.querySelector("p code")?.textContent).toBe("inline");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("hr")).not.toBeNull();
    expect(container.querySelector("pre code")?.textContent).toBe("const complete = true;\n");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "https://mp.weixin.qq.com/s/Dl28D1x2ti1ZfqIBD_axYw",
      "https://example.com/a",
    ]);
    expect(links.every((link) => link.target === "_blank")).toBe(true);
  });

  it("does not activate unsafe links, images, or raw HTML", async () => {
    const container = await renderMarkdown(`
[危险](javascript:alert(1)) [相对](/admin) [安全](mailto:test@example.com)

![远程图片](https://example.com/tracker.png)

<script>alert(1)</script><img src="https://example.com/x" onerror="alert(1)">`);
    const link = container.querySelector("a");

    expect(container.querySelectorAll("a")).toHaveLength(1);
    expect(link?.getAttribute("href")).toBe("mailto:test@example.com");
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.innerHTML).not.toContain("onerror");
    expect(container.textContent).toContain("危险");
    expect(container.textContent).toContain("相对");
    expect(container.textContent).toContain("远程图片");
  });

  it("highlights visible prose and table cells without changing code", async () => {
    const container = await renderMarkdown(
      `## 科技指标

科技内容

- 科技列表

| 平台 | 内容 |
|---|---|
| B站 | 科技类 UP 主 |

\`科技代码\``,
      { highlightTerms: ["科技"] },
    );

    expect(Array.from(container.querySelectorAll("mark")).map((mark) => mark.textContent)).toEqual([
      "科技",
      "科技",
      "科技",
      "科技",
    ]);
    expect(container.querySelector("code")?.textContent).toBe("科技代码");
    expect(container.querySelector("code mark")).toBeNull();
  });

  it("exposes streaming state only while rendering an incomplete response", async () => {
    const streaming = await renderMarkdown("**生成中", { streaming: true });

    expect(streaming.querySelector('[data-markdown-streaming="true"]')).not.toBeNull();
    expect(streaming.textContent).toContain("生成中");
  });
});
