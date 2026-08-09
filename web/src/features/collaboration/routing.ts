export interface ChatRouteTarget {
  kind: "direct" | "group";
  id: string | null;
}

export function parseChatRoute(search: string | URLSearchParams): ChatRouteTarget {
  const params = typeof search === "string" ? new URLSearchParams(search) : search;
  const groupId = cleanRouteId(params.get("group"));
  if (groupId) return { kind: "group", id: groupId };
  return { kind: "direct", id: cleanRouteId(params.get("resume")) };
}

export function directChatSearch(sessionId: string | null): string {
  const params = new URLSearchParams();
  if (sessionId) params.set("resume", sessionId);
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function groupChatSearch(groupId: string): string {
  const params = new URLSearchParams({ group: groupId });
  return `?${params.toString()}`;
}

function cleanRouteId(value: string | null): string | null {
  const cleaned = value?.trim();
  return cleaned || null;
}
