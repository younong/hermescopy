import { useCallback, useEffect, useRef } from "react";
import type { UIEvent } from "react";

const DEFAULT_TOP_THRESHOLD_PX = 200;

type LoadEarlierOptions = {
  autoEnabled: boolean;
  canLoad: boolean;
  loading: boolean;
  onBeforeLoad?: () => void;
  onLoadEarlier?: () => void | Promise<void>;
  resetKey?: string;
  thresholdPx?: number;
};

export function useLoadEarlierOnScroll({
  autoEnabled,
  canLoad,
  loading,
  onBeforeLoad,
  onLoadEarlier,
  resetKey,
  thresholdPx = DEFAULT_TOP_THRESHOLD_PX,
}: LoadEarlierOptions) {
  const previousScrollTopRef = useRef<number | null>(null);
  const requestPendingRef = useRef(false);
  const sawLoadingRef = useRef(false);

  useEffect(() => {
    previousScrollTopRef.current = null;
    requestPendingRef.current = false;
    sawLoadingRef.current = false;
  }, [resetKey]);

  useEffect(() => {
    if (!requestPendingRef.current) return;
    if (loading) {
      sawLoadingRef.current = true;
    } else if (sawLoadingRef.current) {
      requestPendingRef.current = false;
      sawLoadingRef.current = false;
    }
  }, [loading]);

  const load = useCallback((automatic: boolean) => {
    if (
      requestPendingRef.current ||
      loading ||
      !canLoad ||
      !onLoadEarlier ||
      (automatic && !autoEnabled)
    ) {
      return;
    }

    requestPendingRef.current = true;
    onBeforeLoad?.();
    const result = onLoadEarlier();
    if (result && typeof result.then === "function") {
      void result.finally(() => {
        requestPendingRef.current = false;
        sawLoadingRef.current = false;
      });
    }
  }, [autoEnabled, canLoad, loading, onBeforeLoad, onLoadEarlier]);

  const handleScroll = useCallback((event: UIEvent<HTMLElement>) => {
    const scrollTop = event.currentTarget.scrollTop;
    const previousScrollTop = previousScrollTopRef.current;
    previousScrollTopRef.current = scrollTop;

    if (
      previousScrollTop !== null &&
      scrollTop < previousScrollTop &&
      scrollTop <= thresholdPx
    ) {
      load(true);
    }
  }, [load, thresholdPx]);

  const retry = useCallback(() => load(false), [load]);
  const syncScrollPosition = useCallback((scrollTop: number) => {
    previousScrollTopRef.current = scrollTop;
  }, []);

  return { handleScroll, retry, syncScrollPosition };
}
