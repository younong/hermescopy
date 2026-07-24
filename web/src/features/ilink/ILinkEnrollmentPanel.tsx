import type { ILinkEnrollmentStatus } from "@/lib/api";

interface Props {
  error: string;
  ownerLinked?: boolean;
  qrDataUrl: string;
  retry: () => void;
  status: ILinkEnrollmentStatus | "starting";
}

export function ILinkEnrollmentPanel({
  error,
  ownerLinked = false,
  qrDataUrl,
  retry,
  status,
}: Props) {
  const retryable = status === "expired" || status === "failed";

  return (
    <div className="flex flex-col items-center text-center">
      <p className="max-w-sm text-sm text-muted-foreground">
        {ownerLinked
          ? "Scan this one-time code in WeChat. Messages will use this Hermes account's Memory, Skills, and Workspace."
          : "Scan this one-time code in WeChat to connect your private Hermes conversation."}
      </p>
      <div className="mt-5 flex min-h-72 w-full items-center justify-center rounded-xl border border-border bg-muted/20 p-5">
        {status === "starting" && <p aria-busy="true">Creating a secure code…</p>}
        {qrDataUrl && !retryable && status !== "confirmed" && status !== "conflict" && (
          <img
            alt="WeChat enrollment QR code"
            className="h-64 w-64 rounded-xl bg-white p-3"
            src={qrDataUrl}
          />
        )}
        {status === "confirmed" && (
          <div role="status">
            <p className="text-lg font-semibold">Connected</p>
            <p className="mt-2 text-sm text-muted-foreground">
              Send a direct text message in WeChat to continue with this Hermes account.
            </p>
          </div>
        )}
        {status === "conflict" && (
          <div role="alert">
            <p className="text-lg font-semibold">Already connected elsewhere</p>
            <p className="mt-2 text-sm text-muted-foreground">
              {error || "This WeChat account is already connected to another Hermes account."}
            </p>
          </div>
        )}
        {retryable && (
          <div role="alert">
            <p className="text-lg font-semibold">
              {status === "expired" ? "Code expired" : "Could not connect"}
            </p>
            <p className="mt-2 text-sm text-muted-foreground">
              {error || "Create a new code when you are ready."}
            </p>
            <button
              className="mt-5 rounded-lg border border-border px-5 py-2 text-sm font-medium hover:bg-muted"
              onClick={retry}
              type="button"
            >
              Try again
            </button>
          </div>
        )}
      </div>
      {(status === "waiting" || status === "scanned" || status === "registering") && (
        <p className="mt-4 text-sm" role="status">
          {status === "waiting" ? "Waiting for scan…" : "Finishing secure setup…"}
        </p>
      )}
      <p className="mt-5 text-xs text-muted-foreground">
        {ownerLinked
          ? "Preview: direct text messages only. This does not transfer an existing binding."
          : "This preview supports direct text messages only. The code cannot sign in to the dashboard."}
      </p>
    </div>
  );
}
