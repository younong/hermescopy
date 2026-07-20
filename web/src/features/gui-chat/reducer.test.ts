import { describe, expect, it } from "vitest";

import { guiChatReducer } from "./reducer";
import { initialGuiChatState, type GuiChatState, type ImageArtifactState } from "./types";

const RENDERED_TEXT_TRUNCATION_NOTICE =
  "\n\n[… output truncated in Chat GUI to keep the browser responsive …]";

function restoreWithMessage(text: string, info?: { cwd?: string; model?: string }) {
  return guiChatReducer(initialGuiChatState, {
    type: "session.created",
    response: {
      info,
      messages: [{ role: "assistant", text }],
      session_id: "sid",
    },
  });
}

function imageArtifact(state: GuiChatState, id: string): ImageArtifactState {
  const artifact = state.artifacts[id];
  if (!artifact || artifact.kind === "file") throw new Error(`Expected image artifact ${id}`);
  return artifact;
}

describe("guiChatReducer live attach restoration", () => {
  it("restores an in-flight user prompt and partial assistant response", () => {
    const state = guiChatReducer(initialGuiChatState, {
      type: "session.created",
      response: {
        inflight: { assistant: "partial answer", streaming: true, user: "question" },
        messages: [{ role: "assistant", text: "previous" }],
        running: true,
        session_id: "runtime-a",
      },
    });

    expect(state.messages.map(({ role, streaming, text }) => ({ role, streaming, text }))).toEqual([
      { role: "assistant", streaming: undefined, text: "previous" },
      { role: "user", streaming: undefined, text: "question" },
      { role: "assistant", streaming: true, text: "partial answer" },
    ]);
    expect(state.isGenerating).toBe(true);
  });

  it("hydrates an empty streaming assistant turn", () => {
    const state = guiChatReducer(initialGuiChatState, {
      type: "session.created",
      response: {
        inflight: { streaming: true, user: "question" },
        session_id: "runtime-a",
      },
    });

    expect(state.messages.at(-1)).toMatchObject({ role: "assistant", streaming: true, text: "" });
    expect(state.isGenerating).toBe(true);
  });

  it("does not duplicate in-flight content already present in persisted messages", () => {
    const state = guiChatReducer(initialGuiChatState, {
      type: "session.created",
      response: {
        inflight: { assistant: "partial answer", streaming: true, user: "question" },
        messages: [
          { role: "user", text: "question" },
          { role: "assistant", text: "partial answer" },
        ],
        session_id: "runtime-a",
      },
    });

    expect(state.messages).toHaveLength(2);
    expect(state.messages.at(-1)).toMatchObject({
      role: "assistant",
      streaming: true,
      text: "partial answer",
    });
  });

  it("leaves a cold attach without inflight idle", () => {
    const state = guiChatReducer(initialGuiChatState, {
      type: "session.created",
      response: { messages: [{ role: "user", text: "saved" }], session_id: "runtime-a" },
    });

    expect(state.messages).toHaveLength(1);
    expect(state.isGenerating).toBe(false);
  });
});

describe("guiChatReducer history image restoration", () => {
  it("keeps a sent prompt with two WeChat article URLs as plain message text", () => {
    const prompt =
      "分析咿呀咿呀哟喂公众号的两篇文章的阅读量差异化，链接：https://mp.weixin.qq.com/s/Dl28D1x2ti1ZfqIBD_axYw https://mp.weixin.qq.com/s/ZglvujhgYZ7ggnPTlubaBA";
    const state = guiChatReducer(initialGuiChatState, {
      id: "user-1",
      text: prompt,
      type: "user.sent",
    });

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      artifactIds: [],
      id: "user-1",
      role: "user",
      text: prompt,
    });
    expect(state.artifacts).toEqual({});
  });

  it("caps streaming assistant text so large output stays responsive", () => {
    const largeDelta = "x".repeat(130_000);
    const state = guiChatReducer(initialGuiChatState, {
      event: { payload: { text: largeDelta }, type: "message.delta" },
      type: "event",
    });

    expect(state.messages[0].text).toHaveLength(120_000 + RENDERED_TEXT_TRUNCATION_NOTICE.length);
    expect(state.messages[0].text.endsWith(RENDERED_TEXT_TRUNCATION_NOTICE)).toBe(true);

    const afterMoreDelta = guiChatReducer(state, {
      event: { payload: { text: "more" }, type: "message.delta" },
      type: "event",
    });
    expect(afterMoreDelta.messages[0].text).toBe(state.messages[0].text);
  });

  it("caps tool progress output before rendering", () => {
    const withTool = guiChatReducer(initialGuiChatState, {
      event: { payload: { id: "tool-1", name: "WebFetch" }, type: "tool.start" },
      type: "event",
    });
    const state = guiChatReducer(withTool, {
      event: { payload: { text: "x".repeat(130_000) }, type: "tool.progress" },
      type: "event",
    });

    expect(state.toolCalls["tool-1"].output).toHaveLength(
      120_000 + RENDERED_TEXT_TRUNCATION_NOTICE.length,
    );
    expect(state.toolCalls["tool-1"].output.endsWith(RENDERED_TEXT_TRUNCATION_NOTICE)).toBe(true);
  });

  it("caps final tool output before rendering", () => {
    const state = guiChatReducer(initialGuiChatState, {
      event: {
        payload: { id: "tool-1", name: "WebFetch", result_text: "x".repeat(130_000) },
        type: "tool.complete",
      },
      type: "event",
    });

    expect(state.toolCalls["tool-1"].output).toHaveLength(
      120_000 + RENDERED_TEXT_TRUNCATION_NOTICE.length,
    );
    expect(state.toolCalls["tool-1"].output.endsWith(RENDERED_TEXT_TRUNCATION_NOTICE)).toBe(true);
  });

  it("keeps filesystem image tool results downloadable from their original path", () => {
    const withCwd = guiChatReducer(initialGuiChatState, {
      event: { payload: { cwd: "/workspace" }, type: "session.info" },
      type: "event",
    });
    const withTool = guiChatReducer(withCwd, {
      event: { payload: { id: "tool-image", name: "image_generate" }, type: "tool.start" },
      type: "event",
    });
    const state = guiChatReducer(withTool, {
      event: {
        payload: {
          id: "tool-image",
          name: "image_generate",
          result: { image: "outputs/result.png", success: true },
        },
        type: "tool.complete",
      },
      type: "event",
    });

    expect(imageArtifact(state, "tool-image-image")).toMatchObject({
      downloadUrl:
        "/api/files/download?path=outputs%2Fresult.png&cwd=%2Fworkspace&filename=result.png",
      url: "/api/fs/read-data-url?path=outputs%2Fresult.png&cwd=%2Fworkspace",
    });
  });

  it("does not retain large non-rendered tool results in chat state", () => {
    const state = guiChatReducer(initialGuiChatState, {
      event: {
        payload: {
          id: "tool-1",
          name: "terminal",
          output: "done",
          result: { html: "x".repeat(130_000) },
        },
        type: "tool.complete",
      },
      type: "event",
    });

    expect(state.toolCalls["tool-1"].output).toBe("done");
    expect(state.toolCalls["tool-1"].result).toBeUndefined();
  });

  it("replaces object tool inputs with a lightweight display notice", () => {
    const state = guiChatReducer(initialGuiChatState, {
      event: {
        payload: { context: { html: "x".repeat(130_000) }, id: "tool-1", name: "terminal" },
        type: "tool.start",
      },
      type: "event",
    });

    expect(state.toolCalls["tool-1"].input).toBe(
      "[… non-rendered tool result omitted in Chat GUI to keep the browser responsive …]",
    );
  });

  it("restores uploaded files with download URLs", () => {
    const state = guiChatReducer(initialGuiChatState, {
      type: "session.created",
      response: {
        info: { cwd: "/workspace" },
        messages: [{
          attachments: [{
            kind: "pdf",
            mime_type: "application/pdf",
            name: "brief.pdf",
            path: "/workspace/.hermes/desktop-attachments/brief.pdf",
            size_bytes: 123,
          }],
          role: "user",
          text: "",
        }],
        session_id: "sid",
      },
    });

    expect(state.messages[0].attachments?.[0]).toMatchObject({
      downloadUrl: "/api/files/download?path=%2Fworkspace%2F.hermes%2Fdesktop-attachments%2Fbrief.pdf&cwd=%2Fworkspace&filename=brief.pdf",
      name: "brief.pdf",
      sourcePath: "/workspace/.hermes/desktop-attachments/brief.pdf",
    });
  });

  it("turns explicit generated file references into download artifacts", () => {
    const state = restoreWithMessage(
      "Done.\nFull output saved to: outputs/report.html\n[PDF](sandbox:/workspace/report.pdf)",
      { cwd: "/workspace" },
    );

    const artifacts = state.messages[0].artifactIds.map((id) => state.artifacts[id]);
    expect(artifacts).toEqual(expect.arrayContaining([
      expect.objectContaining({
        kind: "file",
        mimeType: "text/html",
        name: "report.html",
        sourcePath: "outputs/report.html",
      }),
      expect.objectContaining({
        kind: "file",
        mimeType: "application/pdf",
        name: "report.pdf",
        sourcePath: "/workspace/report.pdf",
      }),
    ]));
  });

  it("restores a labeled inline HTML path as a download artifact", () => {
    const state = restoreWithMessage(
      "已生成互动版“乌鸦喝水”HTML：\n\n**文件路径：** `/workspace/crow-drinks-water.html`",
      { cwd: "/workspace" },
    );

    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      downloadUrl:
        "/api/files/download?path=%2Fworkspace%2Fcrow-drinks-water.html&cwd=%2Fworkspace&filename=crow-drinks-water.html",
      kind: "file",
      mimeType: "text/html",
      name: "crow-drinks-water.html",
      sourcePath: "/workspace/crow-drinks-water.html",
    });
  });

  it("creates a download artifact when a live assistant message completes with a labeled path", () => {
    const withDelta = guiChatReducer(initialGuiChatState, {
      event: {
        payload: { text: "文件路径： `/workspace/crow-drinks-water.html`" },
        type: "message.delta",
      },
      type: "event",
    });
    const state = guiChatReducer(withDelta, {
      event: {
        payload: { text: "文件路径： `/workspace/crow-drinks-water.html`" },
        type: "message.complete",
      },
      type: "event",
    });

    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      kind: "file",
      mimeType: "text/html",
      name: "crow-drinks-water.html",
      sourcePath: "/workspace/crow-drinks-water.html",
    });
  });

  it("does not treat unlabeled inline code, code blocks, or remote links as generated files", () => {
    const state = restoreWithMessage(
      "普通代码 `/workspace/secret.html`\n```\n文件路径： `/tmp/secret.html`\n```\n[remote](https://example.com/report.pdf)",
    );

    expect(state.messages[0].artifactIds).toEqual([]);
  });

  it("turns a standalone restored image URL into an image artifact", () => {
    const state = restoreWithMessage("生成完成：\nhttps://example.com/cat.png");

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].text).toBe("生成完成：");
    expect(state.messages[0].artifactIds).toHaveLength(1);
    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      messageId: "history-0",
      mimeType: "image/png",
      url: "https://example.com/cat.png",
    });
  });

  it("turns markdown images into image artifacts and removes the markdown image text", () => {
    const state = restoreWithMessage("结果如下：\n![cat](https://example.com/cat.webp)");

    expect(state.messages[0].text).toBe("结果如下：");
    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      mimeType: "image/webp",
      title: "cat",
      url: "https://example.com/cat.webp",
    });
  });

  it("does not treat ordinary bare URLs as images", () => {
    const state = restoreWithMessage("参考：https://example.com/docs");

    expect(state.messages[0].text).toBe("参考：https://example.com/docs");
    expect(state.messages[0].artifactIds).toEqual([]);
    expect(state.artifacts).toEqual({});
  });

  it("does not treat ordinary markdown links as images", () => {
    const state = restoreWithMessage("[docs](https://example.com/docs)");

    expect(state.messages[0].artifactIds).toEqual([]);
    expect(state.artifacts).toEqual({});
  });

  it("recognizes image URLs with query strings and hashes", () => {
    const state = restoreWithMessage("https://cdn.example.com/a.jpg?token=abc#view");

    expect(state.messages[0].artifactIds).toHaveLength(1);
    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      mimeType: "image/jpeg",
      url: "https://cdn.example.com/a.jpg?token=abc#view",
    });
  });

  it("recognizes data image URLs and infers their mime type", () => {
    const dataUrl = "data:image/png;base64,iVBORw0KGgo=";
    const state = restoreWithMessage(dataUrl);

    expect(state.messages[0].artifactIds).toHaveLength(1);
    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      mimeType: "image/png",
      url: dataUrl,
    });
  });

  it("ignores image-looking URLs inside fenced code blocks", () => {
    const state = restoreWithMessage("```txt\nhttps://example.com/cat.png\n```");

    expect(state.messages[0].artifactIds).toEqual([]);
    expect(state.artifacts).toEqual({});
  });

  it("keeps messages that contain only an image URL", () => {
    const state = restoreWithMessage("https://example.com/cat.png");

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].text).toBe("");
    expect(state.messages[0].artifactIds).toHaveLength(1);
  });

  it("extracts images from structured transcript content", () => {
    const state = guiChatReducer(initialGuiChatState, {
      type: "session.created",
      response: {
        messages: [
          {
            content: [
              { text: "看这张图", type: "input_text" },
              { image_url: { url: "https://example.com/a.png" }, type: "image_url" },
            ],
            role: "user",
          },
        ],
        session_id: "sid",
      },
    });

    expect(state.messages[0]).toMatchObject({
      role: "user",
      text: "看这张图",
    });
    expect(state.messages[0].artifactIds).toHaveLength(1);
    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      messageId: "history-0",
      mimeType: "image/png",
      url: "https://example.com/a.png",
    });
  });

  it("restores image, PDF, and file attachment cards from transcript metadata", () => {
    const state = guiChatReducer(initialGuiChatState, {
      type: "session.created",
      response: {
        info: { cwd: "/workspace" },
        messages: [
          {
            attachments: [
              {
                kind: "image",
                mime_type: "image/png",
                name: "shot.png",
                path: "/tmp/shot.png",
                size_bytes: 123,
                source_paths: ["/tmp/shot.png"],
              },
              {
                kind: "pdf",
                mime_type: "application/pdf",
                name: "report.pdf",
                path: "/workspace/.hermes/desktop-attachments/report.pdf",
                pages_attached: 2,
                size_bytes: 456,
                source_paths: ["/tmp/pdf-1.png", "/tmp/pdf-2.png"],
              },
              {
                kind: "file",
                mime_type: "text/plain",
                name: "notes.txt",
                path: "/workspace/notes.txt",
                ref_text: "@file:notes.txt",
                size_bytes: 789,
              },
            ],
            role: "user",
            text: "please inspect",
          },
        ],
        session_id: "sid",
      },
    });

    expect(state.messages[0].attachments).toEqual([
      {
        downloadUrl: "/api/files/download?path=%2Ftmp%2Fshot.png&cwd=%2Fworkspace&filename=shot.png",
        id: "history-0-attachment-0",
        kind: "image",
        mimeType: "image/png",
        name: "shot.png",
        pagesAttached: undefined,
        previewUrl: "/api/fs/read-data-url?path=%2Ftmp%2Fshot.png",
        refText: undefined,
        sizeBytes: 123,
        sourcePath: "/tmp/shot.png",
      },
      {
        downloadUrl: "/api/files/download?path=%2Fworkspace%2F.hermes%2Fdesktop-attachments%2Freport.pdf&cwd=%2Fworkspace&filename=report.pdf",
        id: "history-0-attachment-1",
        kind: "pdf",
        mimeType: "application/pdf",
        name: "report.pdf",
        pagesAttached: 2,
        previewUrl: undefined,
        refText: undefined,
        sizeBytes: 456,
        sourcePath: "/workspace/.hermes/desktop-attachments/report.pdf",
      },
      {
        downloadUrl: "/api/files/download?path=%2Fworkspace%2Fnotes.txt&cwd=%2Fworkspace&filename=notes.txt",
        id: "history-0-attachment-2",
        kind: "file",
        mimeType: "text/plain",
        name: "notes.txt",
        pagesAttached: undefined,
        previewUrl: undefined,
        refText: "@file:notes.txt",
        sizeBytes: 789,
        sourcePath: "/workspace/notes.txt",
      },
    ]);
  });

  it("keeps an attachment-only historical user message", () => {
    const state = guiChatReducer(initialGuiChatState, {
      type: "session.created",
      response: {
        messages: [
          {
            attachments: [{ kind: "file", name: "notes.txt", size_bytes: 12 }],
            role: "user",
            text: "",
          },
        ],
        session_id: "sid",
      },
    });

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].text).toBe("");
    expect(state.messages[0].attachments).toHaveLength(1);
  });

  it("removes attachment prompt hints and avoids duplicate image artifacts", () => {
    const state = guiChatReducer(initialGuiChatState, {
      type: "session.created",
      response: {
        messages: [
          {
            attachments: [
              {
                kind: "image",
                name: "shot.png",
                path: "/tmp/shot.png",
                size_bytes: 123,
                source_paths: ["/tmp/shot.png"],
              },
              {
                kind: "file",
                name: "notes.txt",
                ref_text: "@file:notes.txt",
                size_bytes: 12,
              },
            ],
            role: "user",
            text: "inspect these\n[Image attached at: /tmp/shot.png]\n\n附件：\n@file:notes.txt\n/tmp/shot.png",
          },
        ],
        session_id: "sid",
      },
    });

    expect(state.messages[0].text).toBe("inspect these");
    expect(state.messages[0].artifactIds).toEqual([]);
    expect(state.artifacts).toEqual({});
  });

  it("does not duplicate native image attachments restored from content plus path hint", () => {
    const state = guiChatReducer(initialGuiChatState, {
      type: "session.created",
      response: {
        messages: [
          {
            content: [
              { text: "这张图片里面有什么\n\n[Image attached at: /opt/hermes/shared/.hermes/images/upload.png]", type: "text" },
              { image_url: { url: "data:image/png;base64,iVBORw0KGgo=" }, type: "image_url" },
            ],
            role: "user",
          },
        ],
        session_id: "sid",
      },
    });

    expect(state.messages[0].text).toBe("这张图片里面有什么");
    expect(state.messages[0].artifactIds).toHaveLength(1);
  });

  it("recognizes file URLs as filesystem-backed image artifacts", () => {
    const state = restoreWithMessage("file:///tmp/cat.png");

    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      mimeType: "image/png",
      url: "/api/fs/read-data-url?path=file%3A%2F%2F%2Ftmp%2Fcat.png",
    });
    expect(state.messages[0].text).toBe("");
  });

  it("recognizes POSIX absolute paths as filesystem-backed image artifacts", () => {
    const state = restoreWithMessage("/tmp/cat.png");

    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      mimeType: "image/png",
      url: "/api/fs/read-data-url?path=%2Ftmp%2Fcat.png",
    });
    expect(state.messages[0].text).toBe("");
  });

  it("recognizes local paths with spaces", () => {
    const state = restoreWithMessage("/Users/me/Desktop/my cat.webp");

    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      mimeType: "image/webp",
      url: "/api/fs/read-data-url?path=%2FUsers%2Fme%2FDesktop%2Fmy%20cat.webp",
    });
  });

  it("recognizes home and relative image paths", () => {
    const cwd = "/Users/me/project";
    const home = restoreWithMessage("~/Downloads/a.jpg", { cwd });
    const relative = restoreWithMessage("./outputs/a.png", { cwd });
    const parent = restoreWithMessage("../images/a.jpg", { cwd });
    const nested = restoreWithMessage("outputs/a.webp", { cwd });

    expect(imageArtifact(home, home.messages[0].artifactIds[0]).url).toBe(
      "/api/fs/read-data-url?path=~%2FDownloads%2Fa.jpg",
    );
    expect(imageArtifact(relative, relative.messages[0].artifactIds[0]).url).toBe(
      "/api/fs/read-data-url?path=.%2Foutputs%2Fa.png&cwd=%2FUsers%2Fme%2Fproject",
    );
    expect(imageArtifact(parent, parent.messages[0].artifactIds[0]).url).toBe(
      "/api/fs/read-data-url?path=..%2Fimages%2Fa.jpg&cwd=%2FUsers%2Fme%2Fproject",
    );
    expect(imageArtifact(nested, nested.messages[0].artifactIds[0]).url).toBe(
      "/api/fs/read-data-url?path=outputs%2Fa.webp&cwd=%2FUsers%2Fme%2Fproject",
    );
  });

  it("keeps existing path semantics when no session cwd is available", () => {
    const state = restoreWithMessage("./outputs/a.png");

    expect(imageArtifact(state, state.messages[0].artifactIds[0]).url).toBe(
      "/api/fs/read-data-url?path=.%2Foutputs%2Fa.png",
    );
  });

  it("does not recognize a bare filename as an image path", () => {
    const state = restoreWithMessage("cat.png");

    expect(state.messages[0].artifactIds).toEqual([]);
    expect(state.artifacts).toEqual({});
  });

  it("supports markdown image destinations with angle brackets and spaces", () => {
    const state = restoreWithMessage("结果：\n![cat](</tmp/my cat.png>)");

    expect(state.messages[0].text).toBe("结果：");
    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      title: "cat",
      url: "/api/fs/read-data-url?path=%2Ftmp%2Fmy%20cat.png",
    });
  });

  it("supports markdown image destinations with titles", () => {
    const state = restoreWithMessage('![cat](/tmp/my cat.png "title")');

    expect(state.messages[0].text).toBe("");
    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      title: "cat",
      url: "/api/fs/read-data-url?path=%2Ftmp%2Fmy%20cat.png",
    });
  });

  it("trims Chinese punctuation from image references", () => {
    const remote = restoreWithMessage("https://example.com/cat.png。");
    const local = restoreWithMessage("/tmp/cat.png）");

    expect(imageArtifact(remote, remote.messages[0].artifactIds[0]).url).toBe(
      "https://example.com/cat.png",
    );
    expect(imageArtifact(local, local.messages[0].artifactIds[0]).url).toBe(
      "/api/fs/read-data-url?path=%2Ftmp%2Fcat.png",
    );
  });

  it("normalizes sandbox absolute paths to filesystem-backed artifacts", () => {
    const state = restoreWithMessage("sandbox:/mnt/data/cat.png");

    const artifact = state.artifacts[state.messages[0].artifactIds[0]];
    expect(artifact).toMatchObject({
      url: "/api/fs/read-data-url?path=%2Fmnt%2Fdata%2Fcat.png",
    });
  });

  it("uses cwd from session info events for generated image artifact paths", () => {
    const withCwd = guiChatReducer(initialGuiChatState, {
      event: { payload: { cwd: "/Users/me/project" }, type: "session.info" },
      type: "event",
    });
    const state = guiChatReducer(withCwd, {
      event: {
        payload: { id: "artifact-1", url: "outputs/a.png" },
        type: "artifact.image",
      },
      type: "event",
    });

    expect(state.cwd).toBe("/Users/me/project");
    expect(imageArtifact(state, "artifact-1").url).toBe(
      "/api/fs/read-data-url?path=outputs%2Fa.png&cwd=%2FUsers%2Fme%2Fproject",
    );
  });

  it("creates downloadable file artifacts from structured artifact events", () => {
    const withCwd = guiChatReducer(initialGuiChatState, {
      event: { payload: { cwd: "/Users/me/project" }, type: "session.info" },
      type: "event",
    });
    const withTool = guiChatReducer(withCwd, {
      event: { payload: { id: "tool-1", name: "Write" }, type: "tool.start" },
      type: "event",
    });
    const state = guiChatReducer(withTool, {
      event: {
        payload: {
          id: "artifact-file-1",
          mime_type: "text/html",
          name: "report.html",
          path: "outputs/report.html",
          tool_call_id: "tool-1",
        },
        type: "artifact.created",
      },
      type: "event",
    });

    expect(state.artifacts["artifact-file-1"]).toEqual({
      downloadUrl:
        "/api/files/download?path=outputs%2Freport.html&cwd=%2FUsers%2Fme%2Fproject&filename=report.html",
      id: "artifact-file-1",
      kind: "file",
      messageId: undefined,
      mimeType: "text/html",
      name: "report.html",
      sourcePath: "outputs/report.html",
      toolCallId: "tool-1",
    });
    expect(state.toolCalls["tool-1"].artifactIds).toEqual(["artifact-file-1"]);
  });

  it("keeps structured image artifacts on the image preview path", () => {
    const state = guiChatReducer(initialGuiChatState, {
      event: {
        payload: {
          id: "artifact-image-1",
          mime_type: "image/png",
          path: "/tmp/output.png",
        },
        type: "artifact.created",
      },
      type: "event",
    });

    expect(state.artifacts["artifact-image-1"]).toMatchObject({
      mimeType: "image/png",
      url: "/api/fs/read-data-url?path=%2Ftmp%2Foutput.png",
    });
    expect(state.artifacts["artifact-image-1"].kind).not.toBe("file");
  });

  it("maps generated image cache paths to the static image endpoint", () => {
    const state = restoreWithMessage(
      "/opt/hermes/shared/.hermes/cache/images/apiyi_gpt-image-2-medium_20260705_130933_211cd48c.png",
    );

    expect(imageArtifact(state, state.messages[0].artifactIds[0]).url).toBe(
      "/api/generated-images/apiyi_gpt-image-2-medium_20260705_130933_211cd48c.png",
    );
  });

  it("does not treat non-image paths or unsafe schemes as images", () => {
    for (const text of [
      "/tmp/readme.txt",
      "./docs/guide.md",
      "mailto:test@example.com",
      "javascript:alert(1)",
      "```txt\n/tmp/cat.png\n```",
    ]) {
      const state = restoreWithMessage(text);
      expect(state.messages[0].artifactIds).toEqual([]);
      expect(state.artifacts).toEqual({});
    }
  });
});
