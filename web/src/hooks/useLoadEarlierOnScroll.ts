import { useCallback, useLayoutEffect, useRef } from "react";
import type { UIEvent } from "react";

const DEFAULT_TOP_THRESHOLD_PX = 200;

type LoadEarlierOptions = {
  autoEnabled: boolean;
  canLoad: boolean;
  loading: boolean;
  loadKey?: number | string;
  onBeforeLoad?: () => void;
  onLoadEarlier?: () => void | Promise<void>;
  resetKey?: string;
  thresholdPx?: number;
};

export function useLoadEarlierOnScroll({
  autoEnabled,
  canLoad,
  loading,
  loadKey,
  onBeforeLoad,
  onLoadEarlier,
  resetKey,
  thresholdPx = DEFAULT_TOP_THRESHOLD_PX,
}: LoadEarlierOptions) {
  const previousScrollTopRef = useRef<number | null>(null);
  const requestPendingRef = useRef(false);
  const requestVersionRef = useRef(0);
  const sawLoadingRef = useRef(false);
  const lastLoadKeyRef = useRef<number | string | undefined>(undefined);
  const hasLastLoadKeyRef = useRef(false);

  useLayoutEffect(() => {
    previousScrollTopRef.current = null;
    requestPendingRef.current = false;
    requestVersionRef.current += 1;
    sawLoadingRef.current = false;
    lastLoadKeyRef.current = undefined;
    hasLastLoadKeyRef.current = false;
  }, [resetKey]);

  useLayoutEffect(() => {
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
      loadKey === undefined ||
      !onLoadEarlier ||
      (automatic && (!autoEnabled || (
        hasLastLoadKeyRef.current && lastLoadKeyRef.current === loadKey
      )))
    ) {
      return;
    }

    requestPendingRef.current = true;
    const requestVersion = ++requestVersionRef.current;
    try {
      onBeforeLoad?.();
      const result = onLoadEarlier();
      lastLoadKeyRef.current = loadKey;
      hasLastLoadKeyRef.current = true;
      if (result && typeof result.then === "function") {
        const release = () => {
          if (requestVersionRef.current !== requestVersion) return;
          requestPendingRef.current = false;
          sawLoadingRef.current = false;
        };
        void result.then(release, release);
      }
    } catch (error) {
      if (requestVersionRef.current === requestVersion) {
        requestPendingRef.current = false;
        sawLoadingRef.current = false;
      }
      throw error;
    }
  }, [autoEnabled, canLoad, loadKey, loading, onBeforeLoad, onLoadEarlier]);

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

  const checkTop = useCallback((scrollTop: number) => {
    if (scrollTop <= thresholdPx) load(true);
  }, [load, thresholdPx]);
  const retry = useCallback(() => load(false), [load]);
  const syncScrollPosition = useCallback((scrollTop: number) => {
    previousScrollTopRef.current = scrollTop;
  }, []);

  return { checkTop, handleScroll, retry, syncScrollPosition };
}
