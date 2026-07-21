// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLoadEarlierOnScroll } from "./useLoadEarlierOnScroll";

type HarnessProps = {
  autoEnabled?: boolean;
  canLoad?: boolean;
  loading?: boolean;
  onLoadEarlier: () => void;
  resetKey?: string;
};

function Harness({
  autoEnabled = true,
  canLoad = true,
  loading = false,
  onLoadEarlier,
  resetKey = "session-1",
}: HarnessProps) {
  const { handleScroll, retry } = useLoadEarlierOnScroll({
    autoEnabled,
    canLoad,
    loading,
    onLoadEarlier,
    resetKey,
  });
  return (
    <>
      <div data-testid="scroller" onScroll={handleScroll} />
      <button onClick={retry} type="button">Retry</button>
    </>
  );
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
});

afterEach(() => {
  document.body.innerHTML = "";
});

function scroll(element: HTMLElement, scrollTop: number) {
  element.scrollTop = scrollTop;
  element.dispatchEvent(new Event("scroll", { bubbles: true }));
}

describe("useLoadEarlierOnScroll", () => {
  it("loads once only after a real upward scroll enters the top threshold", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();

    await act(async () => root.render(<Harness onLoadEarlier={onLoadEarlier} />));
    const scroller = container.querySelector<HTMLElement>("[data-testid=scroller]")!;

    await act(async () => scroll(scroller, 0));
    await act(async () => scroll(scroller, 260));
    await act(async () => scroll(scroller, 180));
    await act(async () => scroll(scroller, 120));

    expect(onLoadEarlier).toHaveBeenCalledTimes(1);
    await act(async () => root.unmount());
  });

  it("does not load when automatic loading is disabled or history is unavailable", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();

    await act(async () => root.render(
      <Harness autoEnabled={false} canLoad={false} onLoadEarlier={onLoadEarlier} />,
    ));
    const scroller = container.querySelector<HTMLElement>("[data-testid=scroller]")!;
    await act(async () => scroll(scroller, 300));
    await act(async () => scroll(scroller, 100));

    expect(onLoadEarlier).not.toHaveBeenCalled();
    await act(async () => root.unmount());
  });

  it("allows another page after loading completes and resets between sessions", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();

    await act(async () => root.render(<Harness onLoadEarlier={onLoadEarlier} />));
    let scroller = container.querySelector<HTMLElement>("[data-testid=scroller]")!;
    await act(async () => scroll(scroller, 300));
    await act(async () => scroll(scroller, 100));
    await act(async () => root.render(<Harness loading onLoadEarlier={onLoadEarlier} />));
    await act(async () => root.render(<Harness onLoadEarlier={onLoadEarlier} />));
    scroller = container.querySelector<HTMLElement>("[data-testid=scroller]")!;
    await act(async () => scroll(scroller, 280));
    await act(async () => scroll(scroller, 90));

    expect(onLoadEarlier).toHaveBeenCalledTimes(2);

    await act(async () => root.render(
      <Harness onLoadEarlier={onLoadEarlier} resetKey="session-2" />,
    ));
    scroller = container.querySelector<HTMLElement>("[data-testid=scroller]")!;
    await act(async () => scroll(scroller, 0));
    expect(onLoadEarlier).toHaveBeenCalledTimes(2);
    await act(async () => root.unmount());
  });

  it("supports a guarded manual retry when automatic loading is disabled", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();

    await act(async () => root.render(
      <Harness autoEnabled={false} onLoadEarlier={onLoadEarlier} />,
    ));
    const button = container.querySelector("button")!;
    await act(async () => button.click());
    await act(async () => button.click());

    expect(onLoadEarlier).toHaveBeenCalledTimes(1);
    await act(async () => root.unmount());
  });
});
