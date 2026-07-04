import { describe, expect, it } from "vitest";

import {
  createTerminalInputMouseReportScrubber,
  createTerminalOutputMouseModeScrubber,
  stripMouseTrackingEnableDecset,
} from "./terminalMouseGuards";

describe("stripMouseTrackingEnableDecset", () => {
  it("removes mouse tracking enable modes", () => {
    expect(stripMouseTrackingEnableDecset("a\x1b[?1000hb")).toBe("ab");
    expect(stripMouseTrackingEnableDecset("a\x1b[?1006hb")).toBe("ab");
    expect(stripMouseTrackingEnableDecset("a\x1b[?9hb")).toBe("ab");
    expect(stripMouseTrackingEnableDecset("a\x1b[?2029hb")).toBe("ab");
  });

  it("removes combined mouse modes", () => {
    expect(stripMouseTrackingEnableDecset("a\x1b[?1000;1006hb")).toBe("ab");
  });

  it("preserves non-mouse terminal modes", () => {
    expect(stripMouseTrackingEnableDecset("\x1b[?1004h")).toBe("\x1b[?1004h");
    expect(stripMouseTrackingEnableDecset("\x1b[?2004h")).toBe("\x1b[?2004h");
    expect(stripMouseTrackingEnableDecset("\x1b[?1049h")).toBe("\x1b[?1049h");
    expect(stripMouseTrackingEnableDecset("\x1b[?1000l")).toBe("\x1b[?1000l");
  });

  it("removes only mouse params from mixed mode enables", () => {
    expect(stripMouseTrackingEnableDecset("\x1b[?1000;1004;2004h")).toBe("\x1b[?1004;2004h");
  });
});

describe("createTerminalOutputMouseModeScrubber", () => {
  it("handles split mouse enable sequences", () => {
    const scrubber = createTerminalOutputMouseModeScrubber();

    expect(scrubber.scrubString("a\x1b[?100")).toBe("a");
    expect(scrubber.scrubString("6hb")).toBe("b");
  });

  it("keeps ordinary utf-8 text through the bytes path", () => {
    const scrubber = createTerminalOutputMouseModeScrubber();
    const encoder = new TextEncoder();
    const decoder = new TextDecoder();

    const result = scrubber.scrubBytes(encoder.encode("中文\x1b[?1006htext"));

    expect(decoder.decode(result)).toBe("中文text");
  });
});

describe("createTerminalInputMouseReportScrubber", () => {
  it("strips sgr mouse reports", () => {
    const scrubber = createTerminalInputMouseReportScrubber();

    expect(scrubber.scrub("\x1b[<0;10;20M")).toBe("");
    expect(scrubber.scrub("\x1b[<0;10;20m")).toBe("");
  });

  it("strips mouse reports from mixed chunks", () => {
    const scrubber = createTerminalInputMouseReportScrubber();

    expect(scrubber.scrub("abc\x1b[<0;10;20Mdef")).toBe("abcdef");
  });

  it("handles split sgr mouse reports", () => {
    const scrubber = createTerminalInputMouseReportScrubber();

    expect(scrubber.scrub("abc\x1b[<0;10;")).toBe("abc");
    expect(scrubber.scrub("20Mdef")).toBe("def");
  });

  it("strips urxvt, x10, visible, and bare reports", () => {
    const scrubber = createTerminalInputMouseReportScrubber();

    expect(scrubber.scrub("\x1b[35;10;20M")).toBe("");
    expect(scrubber.scrub("\x1b[Mabc")).toBe("");
    expect(scrubber.scrub("^[[<0;10;20M")).toBe("");
    expect(scrubber.scrub("<0;10;20m")).toBe("");
  });

  it("preserves ordinary input and unrelated csi sequences", () => {
    const scrubber = createTerminalInputMouseReportScrubber();

    expect(scrubber.scrub("hello\r")).toBe("hello\r");
    expect(scrubber.scrub("\x1b[A")).toBe("\x1b[A");
    expect(scrubber.scrub("\x1b[200~paste\x1b[201~")).toBe("\x1b[200~paste\x1b[201~");
  });
});
