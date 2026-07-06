import { describe, expect, it } from "vitest";

import { guiChatReducer } from "./reducer";
import { initialGuiChatState } from "./types";

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

describe("guiChatReducer history image restoration", () => {
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

    expect(home.artifacts[home.messages[0].artifactIds[0]].url).toBe(
      "/api/fs/read-data-url?path=~%2FDownloads%2Fa.jpg",
    );
    expect(relative.artifacts[relative.messages[0].artifactIds[0]].url).toBe(
      "/api/fs/read-data-url?path=.%2Foutputs%2Fa.png&cwd=%2FUsers%2Fme%2Fproject",
    );
    expect(parent.artifacts[parent.messages[0].artifactIds[0]].url).toBe(
      "/api/fs/read-data-url?path=..%2Fimages%2Fa.jpg&cwd=%2FUsers%2Fme%2Fproject",
    );
    expect(nested.artifacts[nested.messages[0].artifactIds[0]].url).toBe(
      "/api/fs/read-data-url?path=outputs%2Fa.webp&cwd=%2FUsers%2Fme%2Fproject",
    );
  });

  it("keeps existing path semantics when no session cwd is available", () => {
    const state = restoreWithMessage("./outputs/a.png");

    expect(state.artifacts[state.messages[0].artifactIds[0]].url).toBe(
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

    expect(remote.artifacts[remote.messages[0].artifactIds[0]].url).toBe(
      "https://example.com/cat.png",
    );
    expect(local.artifacts[local.messages[0].artifactIds[0]].url).toBe(
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
    expect(state.artifacts["artifact-1"].url).toBe(
      "/api/fs/read-data-url?path=outputs%2Fa.png&cwd=%2FUsers%2Fme%2Fproject",
    );
  });

  it("maps generated image cache paths to the static image endpoint", () => {
    const state = restoreWithMessage(
      "/opt/hermes/shared/.hermes/cache/images/apiyi_gpt-image-2-medium_20260705_130933_211cd48c.png",
    );

    expect(state.artifacts[state.messages[0].artifactIds[0]].url).toBe(
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
