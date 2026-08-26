// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLoadEarlierOnScroll } from "../useLoadEarlierOnScroll";

type HarnessProps = {
  autoEnabled?: boolean;
  canLoad?: boolean;
  loading?: boolean;
  loadKey?: number | string;
  onLoadEarlier: () => void | Promise<void>;
  resetKey?: string;
};

function Harness({
  autoEnabled = true,
  canLoad = true,
  loading = false,
  loadKey = "cursor-1",
  onLoadEarlier,
  resetKey = "session-1",
}: HarnessProps) {
  const { checkTop, handleScroll, retry } = useLoadEarlierOnScroll({
    autoEnabled,
    canLoad,
    loading,
    loadKey,
    onLoadEarlier,
    resetKey,
  });
  return (
    <>
      <div data-testid="scroller" onScroll={handleScroll} />
      <button data-testid="check-top" onClick={() => checkTop(0)} type="button">Check top</button>
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

  it("loads at the initial top without requiring a scroll event", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();

    await act(async () => root.render(<Harness onLoadEarlier={onLoadEarlier} />));
    await act(async () => container.querySelector<HTMLElement>("[data-testid=check-top]")?.click());

    expect(onLoadEarlier).toHaveBeenCalledTimes(1);
    await act(async () => root.unmount());
  });

  it("deduplicates a page key and allows the next cursor after loading completes", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();

    await act(async () => root.render(<Harness onLoadEarlier={onLoadEarlier} />));
    const checkTop = container.querySelector<HTMLElement>("[data-testid=check-top]")!;
    await act(async () => {
      checkTop.click();
      checkTop.click();
    });
    await act(async () => root.render(<Harness loading onLoadEarlier={onLoadEarlier} />));
    await act(async () => root.render(<Harness onLoadEarlier={onLoadEarlier} />));
    await act(async () => container.querySelector<HTMLElement>("[data-testid=check-top]")?.click());
    expect(onLoadEarlier).toHaveBeenCalledTimes(1);

    await act(async () => root.render(
      <Harness loadKey="cursor-2" onLoadEarlier={onLoadEarlier} />,
    ));
    await act(async () => container.querySelector<HTMLElement>("[data-testid=check-top]")?.click());

    expect(onLoadEarlier).toHaveBeenCalledTimes(2);
    await act(async () => root.unmount());
  });

  it("does not overlap a pending promise and releases it when it settles", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    let resolveLoad: (() => void) | undefined;
    const onLoadEarlier = vi.fn(() => new Promise<void>((resolve) => {
      resolveLoad = resolve;
    }));

    await act(async () => root.render(<Harness onLoadEarlier={onLoadEarlier} />));
    const checkTop = container.querySelector<HTMLElement>("[data-testid=check-top]")!;
    await act(async () => {
      checkTop.click();
      checkTop.click();
    });
    expect(onLoadEarlier).toHaveBeenCalledTimes(1);

    await act(async () => resolveLoad?.());
    await act(async () => root.render(
      <Harness loadKey="cursor-2" onLoadEarlier={onLoadEarlier} />,
    ));
    await act(async () => container.querySelector<HTMLElement>("[data-testid=check-top]")?.click());

    expect(onLoadEarlier).toHaveBeenCalledTimes(2);
    await act(async () => root.unmount());
  });

  it("allows the same page key again after a synchronous failure", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn()
      .mockImplementationOnce(() => {
        throw new Error("load failed");
      })
      .mockImplementation(() => undefined);
    let checkTop: ((scrollTop: number) => void) | undefined;
    function ThrowHarness() {
      ({ checkTop } = useLoadEarlierOnScroll({
        autoEnabled: true,
        canLoad: true,
        loading: false,
        loadKey: "cursor-1",
        onLoadEarlier,
      }));
      return null;
    }

    await act(async () => root.render(<ThrowHarness />));
    expect(() => checkTop?.(0)).toThrow("load failed");
    checkTop?.(0);

    expect(onLoadEarlier).toHaveBeenCalledTimes(2);
    await act(async () => root.unmount());
  });

  it("does not let an old request release a new reset scope request", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    let resolveFirst: (() => void) | undefined;
    let resolveSecond: (() => void) | undefined;
    const onLoadEarlier = vi.fn()
      .mockImplementationOnce(() => new Promise<void>((resolve) => {
        resolveFirst = resolve;
      }))
      .mockImplementationOnce(() => new Promise<void>((resolve) => {
        resolveSecond = resolve;
      }));

    await act(async () => root.render(<Harness onLoadEarlier={onLoadEarlier} />));
    await act(async () => container.querySelector<HTMLElement>("[data-testid=check-top]")?.click());
    await act(async () => root.render(
      <Harness onLoadEarlier={onLoadEarlier} resetKey="session-2" />,
    ));
    await act(async () => container.querySelector<HTMLElement>("[data-testid=check-top]")?.click());
    expect(onLoadEarlier).toHaveBeenCalledTimes(2);

    await act(async () => resolveFirst?.());
    const retry = Array.from(container.querySelectorAll("button")).find((candidate) =>
      candidate.textContent === "Retry"
    )!;
    await act(async () => retry.click());
    expect(onLoadEarlier).toHaveBeenCalledTimes(2);

    await act(async () => resolveSecond?.());
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

  it("allows the next page key after loading completes and resets scroll state between sessions", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();

    await act(async () => root.render(<Harness onLoadEarlier={onLoadEarlier} />));
    let scroller = container.querySelector<HTMLElement>("[data-testid=scroller]")!;
    await act(async () => scroll(scroller, 300));
    await act(async () => scroll(scroller, 100));
    await act(async () => root.render(<Harness loading onLoadEarlier={onLoadEarlier} />));
    await act(async () => root.render(
      <Harness loadKey="cursor-2" onLoadEarlier={onLoadEarlier} />,
    ));
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

  it("resets automatic page-key deduplication between sessions", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();

    await act(async () => root.render(<Harness onLoadEarlier={onLoadEarlier} />));
    await act(async () => container.querySelector<HTMLElement>("[data-testid=check-top]")?.click());
    await act(async () => root.render(
      <Harness onLoadEarlier={onLoadEarlier} resetKey="session-2" />,
    ));
    await act(async () => container.querySelector<HTMLElement>("[data-testid=check-top]")?.click());

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
    const button = Array.from(container.querySelectorAll("button")).find((candidate) =>
      candidate.textContent === "Retry"
    )!;
    await act(async () => button.click());
    await act(async () => button.click());

    expect(onLoadEarlier).toHaveBeenCalledTimes(1);
    await act(async () => root.unmount());
  });
});
