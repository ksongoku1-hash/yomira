import { afterEach, describe, expect, it } from "vitest";
import { anchorAt, readVerticalAnchor, saveVerticalAnchor, verticalLayout } from "./readerPresentation";

afterEach(() => localStorage.clear());

describe("vertical reading positions", () => {
  it("keeps tall strips at their actual proportions and removes only the webtoon gap", () => {
    const ratios = new Map([[1, 0.1], [2, 1]]);
    const manga = verticalLayout(3, 400, 768, ratios, 1, 12, false);
    expect(manga.heights).toEqual([4012, 412, 768]);
    expect(verticalLayout(3, 400, 768, ratios, 1, 0, false).heights).toEqual([4000, 400, 768]);
    expect(anchorAt(manga.offsets, 4012 + 206)).toEqual({ page: 2, fraction: 0.5 });
    expect(anchorAt(manga.offsets, Number.MAX_SAFE_INTEGER).page).toBe(3);
  });

  it("retains an anchor inside a long page after width and zoom changes", () => {
    const ratios = new Map([[1, 0.1]]);
    const original = verticalLayout(1, 400, 768, ratios, 1, 0, false);
    const anchor = anchorAt(original.offsets, 3000);
    const resized = verticalLayout(1, 800, 768, ratios, 1.5, 0, false);
    expect(resized.heights[0]! * anchor.fraction).toBe(9000);
  });

  it("uses the browser position only when it matches the server page and rejects corrupt data", () => {
    saveVerticalAnchor("chapter", { page: 3, fraction: 0.65 });
    expect(readVerticalAnchor("chapter", 3)).toEqual({ page: 3, fraction: 0.65 });
    expect(readVerticalAnchor("chapter", 2)).toBeNull();
    localStorage.setItem("yomira.reader.vertical-positions.v1", '{"chapter":{"page":3,"fraction":2}}');
    expect(readVerticalAnchor("chapter", 3)).toBeNull();
    localStorage.setItem("yomira.reader.vertical-positions.v1", "broken");
    expect(readVerticalAnchor("chapter", 3)).toBeNull();
  });

  it("bounds stored positions to the fifty most recently saved chapters", () => {
    for (let index = 0; index < 100; index++) saveVerticalAnchor(String(index), { page: 1, fraction: 0.5 });
    expect(Object.keys(JSON.parse(localStorage.getItem("yomira.reader.vertical-positions.v1")!))).toHaveLength(50);
    expect(readVerticalAnchor("0", 1)).toBeNull();
    expect(readVerticalAnchor("99", 1)?.fraction).toBe(0.5);
  });
});
