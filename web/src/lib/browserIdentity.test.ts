import { describe, expect, it, beforeEach } from "vitest";
import { getHermesBrowserId } from "./browserIdentity";

class MemoryStorage {
  private values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  clear(): void {
    this.values.clear();
  }
}

const storage = new MemoryStorage();

describe("getHermesBrowserId", () => {
  beforeEach(() => {
    storage.clear();
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: { localStorage: storage },
    });
  });

  it("uses the global key when owner is unknown", () => {
    const first = getHermesBrowserId();
    const second = getHermesBrowserId(null);
    const third = getHermesBrowserId("");
    const fourth = getHermesBrowserId("   ");

    expect(first).toBe(second);
    expect(third).toBe(first);
    expect(fourth).toBe(first);
    expect(storage.getItem("hermes.dashboard.browser_id")).toBe(first);
  });

  it("buckets browser ids by owner key", () => {
    const ownerA = getHermesBrowserId("ok1_owner_a");
    const ownerAAgain = getHermesBrowserId("ok1_owner_a");
    const ownerB = getHermesBrowserId("ok1_owner_b");

    expect(ownerAAgain).toBe(ownerA);
    expect(ownerB).not.toBe(ownerA);
    expect(storage.getItem("hermes.dashboard.browser_id:ok1_owner_a")).toBe(ownerA);
    expect(storage.getItem("hermes.dashboard.browser_id:ok1_owner_b")).toBe(ownerB);
  });

  it("trims owner keys before choosing the storage bucket", () => {
    const first = getHermesBrowserId("  ok1_owner_a  ");
    const second = getHermesBrowserId("ok1_owner_a");

    expect(second).toBe(first);
    expect(storage.getItem("hermes.dashboard.browser_id:ok1_owner_a")).toBe(first);
    expect(storage.getItem("hermes.dashboard.browser_id:  ok1_owner_a  ")).toBeNull();
  });
});
