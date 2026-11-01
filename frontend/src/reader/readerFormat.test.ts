import { describe, expect, it } from "vitest";
import { detectReaderFormat, formatPreferences } from "./readerFormat";

describe("reader format detection", () => {
  it.each([["Manga", "single", true], ["Manhwa", "vertical", false], ["Cómic", "single", false]] as const)(
    "uses explicit %s metadata", (tag, mode, rtl) => {
      expect(detectReaderFormat([tag], [])?.preferences).toMatchObject({ mode, rtl });
    });
  it("recognizes strips without claiming a country or a type from image geometry", () => {
    expect(detectReaderFormat([], [0.05])).toMatchObject({ label: "Tira vertical", preferences: { mode: "vertical", continuous: true } });
    expect(detectReaderFormat(["Long Strip"], [])?.label).toBe("Tira vertical");
  });
  it("does not guess from ordinary pages, unknown or conflicting metadata", () => {
    expect(detectReaderFormat(["Acción", "Español"], [0.7, 1.5, NaN, Infinity, -1])).toBeNull();
    expect(detectReaderFormat(["Manga", "Comic"], [])).toBeNull();
  });
  it("provides explicit defaults the user can correct", () => {
    expect(formatPreferences("manga")).toEqual({ format: "manga", mode: "single", rtl: true, continuous: false });
    expect(formatPreferences("manhwa")).toEqual({ format: "manhwa", mode: "vertical", rtl: false, continuous: true });
  });
});
