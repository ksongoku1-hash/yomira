import { afterEach, describe, expect, it, vi } from "vitest";
import { readReaderPreferences, saveReaderPreferences, READER_PREFERENCES_KEY } from "./readerPreferences";

afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); });
describe("reader presentation preferences", () => {
  it("restores every presentation preference", () => {
    const preferences = { ...readReaderPreferences(), visibility: "manual" as const,
      immersive: true, progressBar: false, compact: false, shortcut: "c" as const,
      zoom: 2, fit: "height" as const, continuous: true, autoChapter: true };
    expect(saveReaderPreferences(preferences)).toBe(true);
    expect(readReaderPreferences()).toEqual(preferences);
  });
  it.each(["bad json", "null", "[]", '{"visibility":"bad","zoom":999,"fit":"bad","compact":"false"}'])("rejects invalid browser data: %s", (value) => {
    localStorage.setItem(READER_PREFERENCES_KEY, value);
    expect(readReaderPreferences()).toMatchObject({visibility: "always", zoom: 1, fit: null, compact: true});
  });
  it("keeps reading available when browser storage fails", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
    expect(readReaderPreferences().zoom).toBe(1);
    expect(saveReaderPreferences(readReaderPreferences())).toBe(false);
  });
});
