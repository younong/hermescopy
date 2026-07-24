import { useCallback, useEffect, useRef, useState } from "react";
import * as QRCode from "qrcode";
import {
  createEnrollment,
  enrollmentDeviceId,
  getEnrollment,
  type CreatedEnrollment,
  type EnrollmentStatus,
} from "@/lib/publicEnrollmentApi";

const TERMINAL_STATUSES = new Set<EnrollmentStatus>(["confirmed", "expired", "failed"]);

export default function ILinkJoinPage() {
  const [attempt, setAttempt] = useState<CreatedEnrollment | null>(null);
  const [status, setStatus] = useState<EnrollmentStatus | "starting">("starting");
  const [qrDataUrl, setQrDataUrl] = useState("");
  const [error, setError] = useState("");
  const requestGeneration = useRef(0);

  const startEnrollment = useCallback(async () => {
    const generation = ++requestGeneration.current;
    setAttempt(null);
    setQrDataUrl("");
    setError("");
    setStatus("starting");
    try {
      const created = await createEnrollment(enrollmentDeviceId());
      const dataUrl = await QRCode.toDataURL(created.qr_content, {
        errorCorrectionLevel: "M",
        margin: 1,
        width: 256,
      });
      if (generation !== requestGeneration.current) return;
      setAttempt(created);
      setQrDataUrl(dataUrl);
      setStatus(created.status);
    } catch (startError) {
      if (generation !== requestGeneration.current) return;
      setStatus("failed");
      setError(
        startError instanceof Error && startError.message === "too_many_attempts"
          ? "Too many attempts. Please wait a few minutes before trying again."
          : "Enrollment is temporarily unavailable.",
      );
    }
  }, []);

  useEffect(() => {
    void startEnrollment();
    return () => {
      requestGeneration.current += 1;
    };
  }, [startEnrollment]);

  useEffect(() => {
    if (!attempt || TERMINAL_STATUSES.has(status as EnrollmentStatus)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;

    const poll = async () => {
      try {
        const current = await getEnrollment(attempt.attempt_id);
        if (cancelled) return;
        failures = 0;
        setStatus(current.status);
        if (TERMINAL_STATUSES.has(current.status)) return;
      } catch {
        if (cancelled) return;
        failures += 1;
      }
      const delay = Math.min(5_000, 1_000 * 2 ** Math.min(failures, 3));
      timer = setTimeout(() => void poll(), delay);
    };

    timer = setTimeout(() => void poll(), 1_000);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [attempt, status]);

  const retry = () => void startEnrollment();
  const expired = status === "expired" || status === "failed";

  return (
    <main className="min-h-dvh bg-[var(--background)] px-5 py-10 text-[var(--midground)]">
      <section className="mx-auto flex min-h-[calc(100dvh-5rem)] max-w-md flex-col items-center justify-center text-center">
        <p className="mb-3 font-mono text-xs uppercase tracking-[0.24em] opacity-65">Hermes Agent</p>
        <h1 className="font-display text-3xl font-semibold tracking-tight">Connect with WeChat</h1>
        <p className="mt-3 max-w-sm text-sm leading-6 opacity-75">
          Scan this one-time code in WeChat to connect your private Hermes conversation.
        </p>

        <div className="mt-8 flex min-h-72 w-full items-center justify-center rounded-2xl border border-current/15 bg-white/5 p-6">
          {status === "starting" && <p aria-busy="true">Creating a secure code…</p>}
          {qrDataUrl && !expired && status !== "confirmed" && (
            <img
              className="h-64 w-64 rounded-xl bg-white p-3"
              src={qrDataUrl}
              alt="WeChat enrollment QR code"
            />
          )}
          {status === "confirmed" && (
            <div role="status">
              <p className="text-xl font-semibold">Connected</p>
              <p className="mt-2 text-sm opacity-75">Return to WeChat and send a message to continue.</p>
            </div>
          )}
          {expired && (
            <div role="alert">
              <p className="text-lg font-semibold">{status === "expired" ? "Code expired" : "Could not connect"}</p>
              <p className="mt-2 text-sm opacity-75">{error || "Create a new code when you are ready."}</p>
              <button
                className="mt-5 rounded-lg border border-current/30 px-5 py-2 text-sm font-medium hover:bg-white/10"
                type="button"
                onClick={retry}
              >
                Try again
              </button>
            </div>
          )}
        </div>

        {(status === "waiting" || status === "scanned" || status === "registering") && (
          <p className="mt-5 text-sm" role="status">
            {status === "waiting" ? "Waiting for scan…" : "Finishing secure setup…"}
          </p>
        )}
        <p className="mt-8 text-xs leading-5 opacity-55">
          This preview supports direct text messages only. The code expires automatically and cannot sign in to the dashboard.
        </p>
      </section>
    </main>
  );
}
