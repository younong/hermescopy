import { useCallback, useEffect, useRef, useState } from "react";
import * as QRCode from "qrcode";

import type {
  ILinkCreatedEnrollment,
  ILinkEnrollmentState,
  ILinkEnrollmentStatus,
} from "@/lib/api";

const TERMINAL = new Set<ILinkEnrollmentStatus>([
  "confirmed",
  "expired",
  "failed",
  "conflict",
]);

interface EnrollmentOperations {
  create: () => Promise<ILinkCreatedEnrollment>;
  get: (attemptId: string) => Promise<ILinkEnrollmentState>;
}

export function useILinkEnrollment(operations: EnrollmentOperations) {
  const [attempt, setAttempt] = useState<ILinkCreatedEnrollment | null>(null);
  const [status, setStatus] = useState<ILinkEnrollmentStatus | "starting">("starting");
  const [qrDataUrl, setQrDataUrl] = useState("");
  const [error, setError] = useState("");
  const requestGeneration = useRef(0);

  const start = useCallback(async () => {
    const generation = ++requestGeneration.current;
    setAttempt(null);
    setQrDataUrl("");
    setError("");
    setStatus("starting");
    try {
      const created = await operations.create();
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
        startError instanceof Error && startError.message.includes("429")
          ? "Too many attempts. Please wait a few minutes before trying again."
          : "Enrollment is temporarily unavailable.",
      );
    }
  }, [operations]);

  useEffect(() => {
    void start();
    return () => {
      requestGeneration.current += 1;
    };
  }, [start]);

  useEffect(() => {
    if (!attempt || TERMINAL.has(status as ILinkEnrollmentStatus)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;

    const poll = async () => {
      try {
        const current = await operations.get(attempt.attempt_id);
        if (cancelled) return;
        failures = 0;
        setStatus(current.status);
        setError(current.detail ?? "");
        if (TERMINAL.has(current.status)) return;
      } catch (pollError) {
        if (cancelled) return;
        if (pollError instanceof Error && pollError.message.includes("409")) {
          setStatus("conflict");
          setError("This WeChat account is already connected to another Hermes account.");
          return;
        }
        failures += 1;
      }
      timer = setTimeout(
        () => void poll(),
        Math.min(5_000, 1_000 * 2 ** Math.min(failures, 3)),
      );
    };

    timer = setTimeout(() => void poll(), 1_000);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [attempt, operations, status]);

  return { error, qrDataUrl, retry: start, status };
}
