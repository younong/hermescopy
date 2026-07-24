import { HERMES_BASE_PATH, type ILinkEnrollmentStatus } from "./api";

export type EnrollmentStatus = ILinkEnrollmentStatus;

export interface CreatedEnrollment {
  attempt_id: string;
  qr_content: string;
  status: EnrollmentStatus;
  expires_at: number;
}

export interface EnrollmentState {
  status: EnrollmentStatus;
  expires_at: number;
  next_action: "continue_in_wechat" | "retry" | null;
}

async function publicJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${HERMES_BASE_PATH}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!response.ok) {
    throw new Error(response.status === 429 ? "too_many_attempts" : "enrollment_unavailable");
  }
  return (await response.json()) as T;
}

export function createEnrollment(deviceId: string): Promise<CreatedEnrollment> {
  return publicJSON("/api/public/ilink/enrollments", {
    method: "POST",
    body: JSON.stringify({ scene: "join", device_id: deviceId }),
  });
}

export function getEnrollment(attemptId: string): Promise<EnrollmentState> {
  return publicJSON(`/api/public/ilink/enrollments/${encodeURIComponent(attemptId)}`);
}

export function enrollmentDeviceId(): string {
  const key = "hermes-ilink-enrollment-device";
  const existing = window.sessionStorage.getItem(key);
  if (existing) return existing;
  const id = crypto.randomUUID();
  window.sessionStorage.setItem(key, id);
  return id;
}
