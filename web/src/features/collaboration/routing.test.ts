import { describe, expect, it } from "vitest";

import { directChatSearch, groupChatSearch, parseChatRoute } from "./routing";

describe("collaboration routing", () => {
  it("uses direct resume routes without group state", () => {
    expect(parseChatRoute("?resume=session-a")).toEqual({ id: "session-a", kind: "direct" });
    expect(directChatSearch("session-a")).toBe("?resume=session-a");
    expect(directChatSearch(null)).toBe("");
  });

  it("uses group routes and gives them precedence over stale resume parameters", () => {
    expect(parseChatRoute("?resume=session-a&group=group-a")).toEqual({
      id: "group-a",
      kind: "group",
    });
    expect(groupChatSearch("group-a")).toBe("?group=group-a");
  });

  it("does not retain one route's identifier when switching modes", () => {
    expect(parseChatRoute(groupChatSearch("group-a"))).toEqual({ id: "group-a", kind: "group" });
    expect(parseChatRoute(directChatSearch("session-a"))).toEqual({
      id: "session-a",
      kind: "direct",
    });
  });
});
