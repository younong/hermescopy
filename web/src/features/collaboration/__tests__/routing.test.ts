import { describe, expect, it } from "vitest";

import {
  directChatSearch,
  employeeChatSearch,
  groupChatSearch,
  parseChatRoute,
} from "../routing";

describe("collaboration routing", () => {
  it("uses direct resume routes without group state", () => {
    expect(parseChatRoute("?resume=session-a")).toEqual({ id: "session-a", kind: "direct" });
    expect(directChatSearch("session-a")).toBe("?resume=session-a");
    expect(directChatSearch(null)).toBe("");
  });

  it("uses stable employee routes without retaining a session identifier", () => {
    expect(parseChatRoute("?resume=session-a&employee=employee-a")).toEqual({
      id: "employee-a",
      kind: "employee",
    });
    expect(employeeChatSearch("employee-a")).toBe("?employee=employee-a");
  });

  it("uses group routes and gives them precedence over other chat targets", () => {
    expect(parseChatRoute("?resume=session-a&employee=employee-a&group=group-a")).toEqual({
      id: "group-a",
      kind: "group",
    });
    expect(groupChatSearch("group-a")).toBe("?group=group-a");
  });

  it("does not retain one route's identifier when switching modes", () => {
    expect(parseChatRoute(groupChatSearch("group-a"))).toEqual({ id: "group-a", kind: "group" });
    expect(parseChatRoute(employeeChatSearch("employee-a"))).toEqual({
      id: "employee-a",
      kind: "employee",
    });
    expect(parseChatRoute(directChatSearch("session-a"))).toEqual({
      id: "session-a",
      kind: "direct",
    });
  });
});
