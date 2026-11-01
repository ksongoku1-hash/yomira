import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  MAX_READER_BOOKMARKS, MAX_READER_WORKS, READER_LIBRARY_KEY,
  readReaderLibrary, removeReaderBookmark, saveReaderBookmark, saveReaderWorkPreferences,
  type ReaderBookmark, type ReaderWorkPreferences,
} from "./readerLibrary";

const seriesId = "123e4567-e89b-42d3-a456-426614174001";
const chapterId = "123e4567-e89b-42d3-a456-426614174002";
const otherSeriesId = "123e4567-e89b-42d3-a456-426614174003";
const otherChapterId = "123e4567-e89b-42d3-a456-426614174004";
const createdAt = "2026-10-25T17:00:00.000Z";
const profile: ReaderWorkPreferences = { format: "manga", mode: "double", rtl: true, continuous: false };
const bookmark: ReaderBookmark = { seriesId, chapterId, pageIndex: 4, chapterLabel: "Capítulo 3", createdAt };
const workId = (index: number) => `123e4567-e89b-42d3-a456-${index.toString(16).padStart(12, "0")}`;

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("reader library", () => {
  it("starts empty without writing or touching saved reading progress", () => {
    localStorage.setItem("yomira.reader.presentation.v1", "presentation");
    expect(readReaderLibrary()).toEqual({ preferences: [], bookmarks: [], storageError: false });
    expect(localStorage.getItem(READER_LIBRARY_KEY)).toBeNull();
    expect(localStorage.getItem("yomira.reader.presentation.v1")).toBe("presentation");
  });

  it("persists the work choices and bookmarks in one document and replaces only matching records", () => {
    expect(saveReaderWorkPreferences(seriesId, profile)).toBe(true);
    expect(saveReaderWorkPreferences(otherSeriesId, { ...profile, format: "comic", rtl: false })).toBe(true);
    expect(saveReaderBookmark(bookmark)).toBe(true);
    expect(saveReaderBookmark({ ...bookmark, chapterId: otherChapterId })).toBe(true);
    expect(saveReaderWorkPreferences(seriesId, { ...profile, mode: "vertical", continuous: true })).toBe(true);
    expect(saveReaderBookmark({ ...bookmark, chapterLabel: "Capítulo 3 · Marca actualizada" })).toBe(true);
    const saved = readReaderLibrary();
    expect(saved.storageError).toBe(false);
    expect(saved.preferences).toEqual([
      { seriesId, ...profile, mode: "vertical", continuous: true },
      { seriesId: otherSeriesId, ...profile, format: "comic", rtl: false },
    ]);
    expect(saved.bookmarks).toEqual([
      { ...bookmark, chapterLabel: "Capítulo 3 · Marca actualizada" },
      { ...bookmark, chapterId: otherChapterId },
    ]);
  });

  it.each(["auto", "manga", "manhwa", "comic", "custom"] as const)("persists format %s and its mode choices", (format) => {
    expect(saveReaderWorkPreferences(seriesId, { ...profile, format })).toBe(true);
    expect(readReaderLibrary().preferences[0]).toEqual({ ...profile, format, seriesId });
  });

  it("removes only the chosen page in its work and chapter", () => {
    saveReaderWorkPreferences(seriesId, profile);
    saveReaderBookmark(bookmark);
    saveReaderBookmark({ ...bookmark, seriesId: otherSeriesId });
    saveReaderBookmark({ ...bookmark, chapterId: otherChapterId });
    saveReaderBookmark({ ...bookmark, pageIndex: 5 });
    expect(removeReaderBookmark(seriesId, chapterId, 4)).toBe(true);
    expect(readReaderLibrary().bookmarks).toEqual([
      { ...bookmark, seriesId: otherSeriesId }, { ...bookmark, chapterId: otherChapterId }, { ...bookmark, pageIndex: 5 },
    ]);
    expect(readReaderLibrary().preferences).toEqual([{ ...profile, seriesId }]);
    expect(removeReaderBookmark(seriesId, chapterId, 4)).toBe(true);
  });

  it.each([
    "not json", "null", "[]", '{"preferences":[],"bookmarks":null}',
    JSON.stringify({ preferences: [{ ...profile, seriesId: "__proto__" }], bookmarks: [] }),
    JSON.stringify({ preferences: [{ ...profile, seriesId, rtl: "true" }], bookmarks: [] }),
    JSON.stringify({ preferences: [{ ...profile, seriesId, format: ["manga"] }], bookmarks: [] }),
    JSON.stringify({ preferences: [], bookmarks: [{ ...bookmark, pageIndex: -1 }] }),
    JSON.stringify({ preferences: [], bookmarks: [{ ...bookmark, createdAt: "yesterday" }] }),
    JSON.stringify({ preferences: [], bookmarks: [bookmark, bookmark] }),
  ])("reports unreadable saved data and never overwrites it: %s", (raw) => {
    localStorage.setItem(READER_LIBRARY_KEY, raw);
    expect(readReaderLibrary()).toEqual({ preferences: [], bookmarks: [], storageError: true });
    expect(saveReaderWorkPreferences(seriesId, profile)).toBe(false);
    expect(saveReaderBookmark(bookmark)).toBe(false);
    expect(removeReaderBookmark(seriesId, chapterId, 4)).toBe(false);
    expect(localStorage.getItem(READER_LIBRARY_KEY)).toBe(raw);
  });

  it("rejects malformed input without changing existing bookmarks", () => {
    saveReaderBookmark(bookmark);
    const before = localStorage.getItem(READER_LIBRARY_KEY);
    for (const pageIndex of [-1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
      expect(saveReaderBookmark({ ...bookmark, pageIndex })).toBe(false);
      expect(removeReaderBookmark(seriesId, chapterId, pageIndex)).toBe(false);
    }
    expect(saveReaderBookmark({ ...bookmark, chapterLabel: " " })).toBe(false);
    expect(saveReaderBookmark({ ...bookmark, chapterLabel: "x".repeat(501) })).toBe(false);
    expect(saveReaderBookmark({ ...bookmark, createdAt: "2026-02-30T17:00:00.000Z" })).toBe(false);
    expect(saveReaderWorkPreferences("not-a-uuid", profile)).toBe(false);
    expect(saveReaderWorkPreferences(seriesId, { ...profile, mode: "invalid" } as unknown as ReaderWorkPreferences)).toBe(false);
    expect(localStorage.getItem(READER_LIBRARY_KEY)).toBe(before);
  });

  it("rejects additional works at the shared limit without evicting profiles or bookmarks", () => {
    const preferences = Array.from({ length: MAX_READER_WORKS }, (_, index) => ({ ...profile, seriesId: workId(index) }));
    localStorage.setItem(READER_LIBRARY_KEY, JSON.stringify({ preferences, bookmarks: [] }));
    expect(saveReaderWorkPreferences(workId(MAX_READER_WORKS), profile)).toBe(false);
    expect(saveReaderBookmark({ ...bookmark, seriesId: workId(MAX_READER_WORKS) })).toBe(false);
    expect(readReaderLibrary().preferences).toEqual(preferences);
    expect(saveReaderWorkPreferences(workId(0), { ...profile, rtl: false })).toBe(true);
    expect(saveReaderBookmark({ ...bookmark, seriesId: workId(0) })).toBe(true);
    expect(readReaderLibrary().bookmarks).toHaveLength(1);
  });

  it("rejects excess bookmarks but allows updating and removing existing ones at capacity", () => {
    const bookmarks = Array.from({ length: MAX_READER_BOOKMARKS }, (_, pageIndex) => ({ ...bookmark, pageIndex }));
    localStorage.setItem(READER_LIBRARY_KEY, JSON.stringify({ preferences: [], bookmarks }));
    expect(saveReaderBookmark({ ...bookmark, pageIndex: MAX_READER_BOOKMARKS })).toBe(false);
    expect(readReaderLibrary().bookmarks).toEqual(bookmarks);
    expect(saveReaderBookmark({ ...bookmark, chapterLabel: "Página marcada" })).toBe(true);
    expect(readReaderLibrary().bookmarks[4]?.chapterLabel).toBe("Página marcada");
    expect(removeReaderBookmark(seriesId, chapterId, 0)).toBe(true);
    expect(saveReaderBookmark({ ...bookmark, pageIndex: MAX_READER_BOOKMARKS })).toBe(true);
    expect(readReaderLibrary().bookmarks).toHaveLength(MAX_READER_BOOKMARKS);
  });

  it("rejects oversized stored documents without replacing them", () => {
    const raw = JSON.stringify({ preferences: [], bookmarks: [], padding: "x".repeat(2_000_000) });
    localStorage.setItem(READER_LIBRARY_KEY, raw);
    expect(readReaderLibrary().storageError).toBe(true);
    expect(saveReaderBookmark(bookmark)).toBe(false);
    expect(localStorage.getItem(READER_LIBRARY_KEY)).toBe(raw);
  });

  it("does not crash when reading storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Blocked"); });
    expect(readReaderLibrary().storageError).toBe(true);
    expect(saveReaderWorkPreferences(seriesId, profile)).toBe(false);
    expect(saveReaderBookmark(bookmark)).toBe(false);
    expect(removeReaderBookmark(seriesId, chapterId, 4)).toBe(false);
  });

  it("retains the previous document if an atomic storage write fails", () => {
    saveReaderBookmark(bookmark);
    const before = localStorage.getItem(READER_LIBRARY_KEY);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Quota"); });
    expect(saveReaderWorkPreferences(seriesId, profile)).toBe(false);
    expect(saveReaderBookmark({ ...bookmark, pageIndex: 9 })).toBe(false);
    expect(removeReaderBookmark(seriesId, chapterId, 4)).toBe(false);
    expect(localStorage.getItem(READER_LIBRARY_KEY)).toBe(before);
    expect(readReaderLibrary().bookmarks).toEqual([bookmark]);
  });
});
