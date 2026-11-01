export type WorkFormat = "auto" | "manga" | "manhwa" | "comic" | "custom";

export interface ReaderWorkPreferences {
  format: WorkFormat;
  mode: "single" | "vertical" | "double";
  rtl: boolean;
  continuous: boolean;
}

export interface ReaderWorkProfile extends ReaderWorkPreferences {
  seriesId: string;
}

export interface ReaderBookmark {
  seriesId: string;
  chapterId: string;
  pageIndex: number;
  chapterLabel: string;
  createdAt: string;
}

export interface ReaderLibrary {
  preferences: ReaderWorkProfile[];
  bookmarks: ReaderBookmark[];
}

export interface ReaderLibraryState extends ReaderLibrary {
  storageError: boolean;
}

export const READER_LIBRARY_KEY = "yomira.reader.library.v1";
export const MAX_READER_WORKS = 200;
export const MAX_READER_BOOKMARKS = 500;
const MAX_STORAGE_CHARACTERS = 2_000_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function id(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

function validProfile(value: unknown): value is ReaderWorkProfile {
  return record(value) && id(value.seriesId) &&
    typeof value.format === "string" && ["auto", "manga", "manhwa", "comic", "custom"].includes(value.format) &&
    typeof value.mode === "string" && ["single", "vertical", "double"].includes(value.mode) &&
    typeof value.rtl === "boolean" && typeof value.continuous === "boolean";
}

function validBookmark(value: unknown): value is ReaderBookmark {
  if (!record(value) || !id(value.seriesId) || !id(value.chapterId) ||
    typeof value.pageIndex !== "number" || !Number.isSafeInteger(value.pageIndex) || value.pageIndex < 0 ||
    typeof value.chapterLabel !== "string" || !value.chapterLabel.trim() || value.chapterLabel.length > 500 ||
    typeof value.createdAt !== "string" || value.createdAt.length > 32 || !Number.isFinite(Date.parse(value.createdAt))) return false;
  return new Date(value.createdAt).toISOString() === value.createdAt;
}

function bookmarkKey(value: Pick<ReaderBookmark, "seriesId" | "chapterId" | "pageIndex">): string {
  return `${value.seriesId}:${value.chapterId}:${value.pageIndex}`;
}

export function validLibrary(value: unknown): value is ReaderLibrary {
  if (!record(value) || !Array.isArray(value.preferences) || !Array.isArray(value.bookmarks) ||
    value.preferences.length > MAX_READER_WORKS || value.bookmarks.length > MAX_READER_BOOKMARKS ||
    !value.preferences.every(validProfile) || !value.bookmarks.every(validBookmark)) return false;
  return new Set(value.preferences.map((item) => item.seriesId)).size === value.preferences.length &&
    new Set(value.bookmarks.map(bookmarkKey)).size === value.bookmarks.length &&
    new Set([...value.preferences, ...value.bookmarks].map((item) => item.seriesId)).size <= MAX_READER_WORKS;
}

export function parseReaderLibrary(raw: string | null): ReaderLibraryState {
  try {
    if (raw === null) return { preferences: [], bookmarks: [], storageError: false };
    if (raw.length <= MAX_STORAGE_CHARACTERS) {
      const saved: unknown = JSON.parse(raw);
      if (validLibrary(saved)) return { preferences: saved.preferences, bookmarks: saved.bookmarks, storageError: false };
    }
  } catch { /* Reading must remain possible when storage is unavailable. */ }
  return { preferences: [], bookmarks: [], storageError: true };
}

export function readReaderLibrary(): ReaderLibraryState {
  try { return parseReaderLibrary(localStorage.getItem(READER_LIBRARY_KEY)); }
  catch { return { preferences: [], bookmarks: [], storageError: true }; }
}

function saveLibrary(value: ReaderLibrary): boolean {
  if (!validLibrary(value)) return false;
  try {
    // shortcut: simultaneous edits in two tabs can race; use transactions if multi-tab editing is added.
    localStorage.setItem(READER_LIBRARY_KEY, JSON.stringify(value));
    return true;
  } catch { return false; }
}

export function saveReaderWorkPreferences(seriesId: string, preferences: ReaderWorkPreferences): boolean {
  const profile = { ...preferences, seriesId };
  if (!validProfile(profile)) return false;
  const saved = readReaderLibrary();
  if (saved.storageError) return false;
  const index = saved.preferences.findIndex((item) => item.seriesId === seriesId);
  if (index < 0) saved.preferences.push(profile);
  else saved.preferences[index] = profile;
  return saveLibrary({ preferences: saved.preferences, bookmarks: saved.bookmarks });
}

export function saveReaderBookmark(bookmark: ReaderBookmark): boolean {
  if (!validBookmark(bookmark)) return false;
  const saved = readReaderLibrary();
  if (saved.storageError) return false;
  const key = bookmarkKey(bookmark);
  const index = saved.bookmarks.findIndex((item) => bookmarkKey(item) === key);
  if (index < 0) saved.bookmarks.push(bookmark);
  else saved.bookmarks[index] = bookmark;
  return saveLibrary({ preferences: saved.preferences, bookmarks: saved.bookmarks });
}

export function removeReaderBookmark(seriesId: string, chapterId: string, pageIndex: number): boolean {
  if (!id(seriesId) || !id(chapterId) || !Number.isSafeInteger(pageIndex) || pageIndex < 0) return false;
  const saved = readReaderLibrary();
  if (saved.storageError) return false;
  const key = bookmarkKey({ seriesId, chapterId, pageIndex });
  const bookmarks = saved.bookmarks.filter((item) => bookmarkKey(item) !== key);
  if (bookmarks.length === saved.bookmarks.length) return true;
  return saveLibrary({ preferences: saved.preferences, bookmarks });
}
