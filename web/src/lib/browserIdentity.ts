const BROWSER_ID_KEY = "hermes.dashboard.browser_id";

function randomBrowserId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `browser-${crypto.randomUUID()}`;
  }

  return `browser-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
}

export function getHermesBrowserId(): string {
  if (typeof window === "undefined") {
    return randomBrowserId();
  }

  try {
    const existing = window.localStorage.getItem(BROWSER_ID_KEY);

    if (existing) {
      return existing;
    }

    const next = randomBrowserId();
    window.localStorage.setItem(BROWSER_ID_KEY, next);

    return next;
  } catch {
    return randomBrowserId();
  }
}
