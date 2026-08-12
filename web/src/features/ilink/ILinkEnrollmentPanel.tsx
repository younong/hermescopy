import { guiChatTranslations, useI18n } from "@/i18n";
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
  const { t } = useI18n();
  const copy = guiChatTranslations(t).weChat;
  const retryable = status === "expired" || status === "failed";

  return (
    <div className="flex flex-col items-center text-center">
      <p className="max-w-sm text-sm text-muted-foreground">
        {ownerLinked
          ? copy.employeeDescription
          : copy.directDescription}
      </p>
      <div className="mt-5 flex min-h-72 w-full items-center justify-center rounded-xl border border-border bg-muted/20 p-5">
        {status === "starting" && <p aria-busy="true">{copy.creatingCode}</p>}
        {qrDataUrl && !retryable && status !== "confirmed" && status !== "conflict" && (
          <img
            alt={copy.qrCode}
            className="h-64 w-64 rounded-xl bg-white p-3"
            src={qrDataUrl}
          />
        )}
        {status === "confirmed" && (
          <div role="status">
            <p className="text-lg font-semibold">{copy.connected}</p>
            <p className="mt-2 text-sm text-muted-foreground">{copy.connectedHint}</p>
          </div>
        )}
        {status === "conflict" && (
          <div role="alert">
            <p className="text-lg font-semibold">{copy.alreadyConnectedTitle}</p>
            <p className="mt-2 text-sm text-muted-foreground">
              {error || copy.alreadyConnected}
            </p>
          </div>
        )}
        {retryable && (
          <div role="alert">
            <p className="text-lg font-semibold">
              {status === "expired" ? copy.codeExpired : copy.connectFailed}
            </p>
            <p className="mt-2 text-sm text-muted-foreground">
              {error || copy.createNewCode}
            </p>
            <button
              className="mt-5 rounded-lg border border-border px-5 py-2 text-sm font-medium hover:bg-muted"
              onClick={retry}
              type="button"
            >
              {copy.tryAgain}
            </button>
          </div>
        )}
      </div>
      {(status === "waiting" || status === "scanned" || status === "registering") && (
        <p className="mt-4 text-sm" role="status">
          {status === "waiting" ? copy.waitingForScan : copy.finishingSetup}
        </p>
      )}
      <p className="mt-5 text-xs text-muted-foreground">
        {ownerLinked
          ? copy.employeePreview
          : copy.directPreview}
      </p>
    </div>
  );
}
