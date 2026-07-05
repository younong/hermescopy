const MOUSE_DECSET_MODES = new Set([
  "9",
  "1000",
  "1001",
  "1002",
  "1003",
  "1005",
  "1006",
  "1015",
  "1016",
  "2029",
]);

export function isDashboardMouseMode(mode: number | string): boolean {
  return MOUSE_DECSET_MODES.has(String(mode));
}

const OUTPUT_PENDING_LIMIT = 64;
const INPUT_PENDING_LIMIT = 96;

export const DISABLE_DASHBOARD_MOUSE_MODES =
  "\x1b[?2029l" +
  "\x1b[?1016l" +
  "\x1b[?1015l" +
  "\x1b[?1006l" +
  "\x1b[?1005l" +
  "\x1b[?1003l" +
  "\x1b[?1002l" +
  "\x1b[?1001l" +
  "\x1b[?1000l" +
  "\x1b[?9l";

function splitMouseDecsetTail(data: string): [string, string] {
  const match = data.match(/\x1b(?:\[\??[0-9;]*)?$/);
  if (!match?.index || data.length - match.index > OUTPUT_PENDING_LIMIT) {
    if (match?.index === 0 && data.length <= OUTPUT_PENDING_LIMIT) {
      return ["", data];
    }
    return [data, ""];
  }
  return [data.slice(0, match.index), data.slice(match.index)];
}

export function stripMouseTrackingEnableDecset(data: string): string {
  return data.replace(/\x1b\[\?([0-9;]+)h/g, (sequence, rawModes: string) => {
    const modes = rawModes.split(";");
    if (modes.some((mode) => !/^\d+$/.test(mode))) {
      return sequence;
    }

    const kept = modes.filter((mode) => !MOUSE_DECSET_MODES.has(mode));
    return kept.length === 0 ? "" : `\x1b[?${kept.join(";")}h`;
  });
}

function stripInputMouseReports(data: string): string {
  return data
    .replace(/\x1b\[<\d+;\d+;\d+[Mm]/g, "")
    .replace(/\x1b\[\d+;\d+;\d+[Mm]/g, "")
    .replace(/\x1b\[M[\s\S]{3}/g, "")
    .replace(/\^\[\[<\d+;\d+;\d+[Mm]/g, "")
    .replace(/\^\[\[\d+;\d+;\d+[Mm]/g, "")
    .replace(/<\d+;\d+;\d+[Mm]/g, "")
    .replace(/\d+;\d+;\d+[Mm]/g, "");
}

function isIncompleteInputMouseReportPrefix(data: string): boolean {
  return (
    /^\x1b(?:\[)?$/.test(data) ||
    /^\x1b\[<\d*(?:;\d*){0,2}$/.test(data) ||
    /^\x1b\[\d*(?:;\d*){0,2}$/.test(data) ||
    /^\x1b\[M[\s\S]{0,2}$/.test(data) ||
    /^\^\[\[<\d*(?:;\d*){0,2}$/.test(data) ||
    /^\^\[\[\d*(?:;\d*){0,2}$/.test(data) ||
    /^\d+(?:;\d*){0,2}$/.test(data)
  );
}

function splitInputMouseReportTail(data: string): [string, string] {
  const minStart = Math.max(0, data.length - INPUT_PENDING_LIMIT);
  const starts: number[] = [];

  for (let i = data.length - 1; i >= minStart; i -= 1) {
    const ch = data[i];
    if (ch === "\x1b" || ch === "^" || ch === "<") {
      starts.push(i);
    }
  }

  for (const start of starts) {
    const tail = data.slice(start);
    if (isIncompleteInputMouseReportPrefix(tail)) {
      return [data.slice(0, start), tail];
    }
  }

  return [data, ""];
}

export function createTerminalOutputMouseModeScrubber() {
  let pending = "";
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();

  const scrubString = (data: string): string => {
    const [complete, tail] = splitMouseDecsetTail(pending + data);
    pending = tail;
    return stripMouseTrackingEnableDecset(complete);
  };

  return {
    scrubString,
    scrubBytes(data: Uint8Array): Uint8Array {
      const decoded = decoder.decode(data, { stream: true });
      const scrubbed = scrubString(decoded);
      return encoder.encode(scrubbed);
    },
    flushString(): string {
      const flushed = stripMouseTrackingEnableDecset(pending);
      pending = "";
      return flushed;
    },
    flushBytes(): Uint8Array {
      const decoded = decoder.decode();
      const flushed = stripMouseTrackingEnableDecset(pending + decoded);
      pending = "";
      return encoder.encode(flushed);
    },
  };
}

export function createTerminalInputMouseReportScrubber() {
  let pending = "";

  return {
    scrub(data: string): string {
      const [complete, tail] = splitInputMouseReportTail(pending + data);
      pending = tail;
      return stripInputMouseReports(complete);
    },
    flush(): string {
      const flushed = stripInputMouseReports(pending);
      pending = "";
      return flushed;
    },
  };
}
