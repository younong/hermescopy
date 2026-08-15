export type ChatRouteTarget =
  | { kind: "direct"; id: string | null }
  | { kind: "employee" | "group"; id: string };

export function parseChatRoute(search: string | URLSearchParams): ChatRouteTarget {
  const params = typeof search === "string" ? new URLSearchParams(search) : search;
  const groupId = cleanRouteId(params.get("group"));
  if (groupId) return { kind: "group", id: groupId };
  const employeeId = cleanRouteId(params.get("employee"));
  if (employeeId) return { kind: "employee", id: employeeId };
  return { kind: "direct", id: cleanRouteId(params.get("resume")) };
}

export function directChatSearch(sessionId: string | null): string {
  const params = new URLSearchParams();
  if (sessionId) params.set("resume", sessionId);
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function employeeChatSearch(employeeId: string): string {
  const params = new URLSearchParams({ employee: employeeId });
  return `?${params.toString()}`;
}

export function groupChatSearch(groupId: string): string {
  const params = new URLSearchParams({ group: groupId });
  return `?${params.toString()}`;
}

function cleanRouteId(value: string | null): string | null {
  const cleaned = value?.trim();
  return cleaned || null;
}
