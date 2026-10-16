import { afterEach, describe, expect, it } from "vitest";
import { anchorAt, fittedPageWidth, readVerticalAnchor, saveVerticalAnchor, verticalLayout } from "./readerPresentation";

afterEach(() => localStorage.clear());

describe("pages fitted to the reading surface", () => {
  it("fits portrait, landscape and tall pages while giving zoom real scrollable dimensions", () => {
    expect(fittedPageWidth(2 / 3, 1000, 600, null, 1)).toBe(400);
    expect(fittedPageWidth(2, 1000, 600, null, 1)).toBe(1000);
    expect(fittedPageWidth(0.1, 400, 600, "width", 1)).toBe(400);
    expect(fittedPageWidth(2 / 3, 1000, 600, null, 2)).toBe(800);
    expect(fittedPageWidth(2, 400, 600, "height", 1)).toBe(1200);
    expect(fittedPageWidth(undefined, 1000, 600, null, 1)).toBeUndefined();
    expect(fittedPageWidth(1, 0, 0, null, 1)).toBeUndefined();
  });
});

describe("vertical reading positions", () => {
  it("keeps the selected page when a browser rounds its fractional offset", () => {
    expect(anchorAt([0, 2033.25, 4000], 2033)).toEqual({ page: 2, fraction: 0 });
    expect(anchorAt([0, 2033.25, 4000], 2032).page).toBe(1);
  });
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
