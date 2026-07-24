import { useMemo } from "react";

import { ILinkEnrollmentPanel } from "@/features/ilink/ILinkEnrollmentPanel";
import { useILinkEnrollment } from "@/features/ilink/useILinkEnrollment";
import {
  createEnrollment,
  enrollmentDeviceId,
  getEnrollment,
} from "@/lib/publicEnrollmentApi";

export default function ILinkJoinPage() {
  const operations = useMemo(
    () => ({
      create: () => createEnrollment(enrollmentDeviceId()),
      get: getEnrollment,
    }),
    [],
  );
  const enrollment = useILinkEnrollment(operations);

  return (
    <main className="min-h-dvh bg-[var(--background)] px-5 py-10 text-[var(--midground)]">
      <section className="mx-auto flex min-h-[calc(100dvh-5rem)] max-w-md flex-col items-center justify-center text-center">
        <p className="mb-3 font-mono text-xs uppercase tracking-[0.24em] opacity-65">Hermes Agent</p>
        <h1 className="font-display text-3xl font-semibold tracking-tight">Connect with WeChat</h1>
        <div className="mt-5 w-full">
          <ILinkEnrollmentPanel {...enrollment} />
        </div>
      </section>
    </main>
  );
}
