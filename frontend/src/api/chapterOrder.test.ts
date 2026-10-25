import { describe, expect, it } from "vitest";
import type { ConnectorChapter } from "./client";
import { currentConnectorChapter } from "./chapterOrder";

const item = (remoteId: string, language = "es"): ConnectorChapter => ({ remoteId, language,
  displayLabel: "Capítulo 16", remoteLabel: null, chapterRawLabel: "16", chapterSortKey: [16] });
describe("current source chapter", () => {
  it("prefers the original reference even when labels repeat", () => {
    const exact = item("exact");
    expect(currentConnectorChapter([item("other"), exact], "exact", "Capítulo 16")).toBe(exact);
  });
  it("uses only a unique exact label in the same source catalog", () => {
    const fresh = item("fresh");
    expect(currentConnectorChapter([fresh], "expired", "Capítulo 16")).toBe(fresh);
    expect(currentConnectorChapter([fresh], "expired", "Capítulo 1")).toBeUndefined();
  });
  it("rejects ambiguous matches across languages and editions", () => {
    expect(currentConnectorChapter([item("one"),item("two", "en")], "expired", "Capítulo 16")).toBeUndefined();
    expect(currentConnectorChapter([item("one"),item("two")], "expired", "Capítulo 16")).toBeUndefined();
  });
});
