import { requestJson } from "./client";
import { parseReaderLibrary, validLibrary, READER_LIBRARY_KEY, type ReaderBookmark, type ReaderLibraryState, type ReaderWorkPreferences } from "../reader/readerLibrary";

export interface StoredReaderLibrary extends ReaderLibraryState { libraryId: string; migrationWarning: string | null }
export const READER_LIBRARY_QUERY_KEY = ["reader-library"] as const;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const record = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
function invalid(): never { throw new Error("La API devolvió datos de marcadores no válidos. Los datos anteriores se conservan."); }
function parse(payload: unknown): StoredReaderLibrary {
  if (!record(payload) || !record(payload.data) || !record(payload.meta)) return invalid();
  const data = payload.data;
  if (typeof data.library_id !== "string" || !UUID.test(data.library_id) || !Array.isArray(data.preferences) || !Array.isArray(data.bookmarks) ||
    !data.preferences.every(record) || !data.bookmarks.every(record)) return invalid();
  const library = {
    preferences: data.preferences.map((item) => ({ seriesId: item.series_id, format: item.format, mode: item.mode, rtl: item.rtl, continuous: item.continuous })),
    bookmarks: data.bookmarks.map((item) => ({ seriesId: item.series_id, chapterId: item.chapter_id, pageIndex: item.page_index,
      chapterLabel: item.chapter_label, createdAt: item.created_at })),
  };
  if (!validLibrary(library)) return invalid();
  return { ...library, libraryId: data.library_id, storageError: false, migrationWarning: null };
}
const profileBody = (value: ReaderWorkPreferences) => ({ format: value.format, mode: value.mode, rtl: value.rtl, continuous: value.continuous });
const bookmarkBody = (value: ReaderBookmark) => ({ series_id: value.seriesId, chapter_id: value.chapterId, page_index: value.pageIndex,
  chapter_label: value.chapterLabel, created_at: value.createdAt });
async function getLibrary(signal?: AbortSignal): Promise<StoredReaderLibrary> {
  const { payload } = await requestJson("/reader/library", signal ? { signal } : {});
  return parse(payload);
}

// Serialize writes in this browser; the database merges individual records across browsers.
let writeTail: Promise<void> = Promise.resolve();
function write(path: string, method: "PUT" | "POST" | "DELETE", body?: unknown): Promise<StoredReaderLibrary> {
  const operation = writeTail.catch(() => undefined).then(async () => {
    const { payload } = await requestJson(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    return parse(payload);
  });
  writeTail = operation.then(() => undefined, () => undefined);
  return operation;
}
let loading: Promise<StoredReaderLibrary> | null = null;
export function loadReaderLibrary(): Promise<StoredReaderLibrary> {
  if (loading) return loading;
  loading = (async () => {
    const server = await getLibrary();
    let raw: string | null;
    try { raw = localStorage.getItem(READER_LIBRARY_KEY); }
    catch { return { ...server, migrationWarning: "No se pudo leer la copia anterior del navegador. Los nuevos datos se guardan en tu biblioteca." }; }
    if (raw === null) return server;
    const legacy = parseReaderLibrary(raw);
    if (legacy.storageError) return { ...server, migrationWarning: "La copia anterior del navegador no es válida. Se conserva intacta; no se importó." };
    if (!legacy.preferences.length && !legacy.bookmarks.length) return server;
    const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(raw));
    const migrationId = Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
    const receipt = `yomira.reader.import.${server.libraryId}.${migrationId}`;
    try { if (localStorage.getItem(receipt) === "complete") return server; } catch { /* Server ledger still makes imports idempotent. */ }
    try {
      const imported = await write("/reader/library/import", "POST", { migration_id: migrationId,
        preferences: legacy.preferences.map((value) => ({ series_id: value.seriesId, ...profileBody(value) })),
        bookmarks: legacy.bookmarks.map(bookmarkBody) });
      if (imported.libraryId !== server.libraryId) return invalid();
      try { localStorage.setItem(receipt, "complete"); } catch { /* Keep the original; the server remembers this import. */ }
      return imported;
    } catch (cause) {
      throw new Error("No se pudieron trasladar los marcadores anteriores a esta biblioteca. La copia del navegador se conserva; puedes reintentar.", { cause });
    }
  })().finally(() => { loading = null; });
  return loading;
}
export async function putReaderWorkPreferences(seriesId: string, value: ReaderWorkPreferences): Promise<StoredReaderLibrary> {
  if (!UUID.test(seriesId)) throw new RangeError("Obra no válida");
  await loadReaderLibrary();
  return write(`/reader/library/preferences/${seriesId}`, "PUT", profileBody(value));
}
export async function putReaderBookmark(value: ReaderBookmark): Promise<StoredReaderLibrary> {
  await loadReaderLibrary();
  return write("/reader/library/bookmarks", "PUT", bookmarkBody(value));
}
export async function deleteReaderBookmark(chapterId: string, pageIndex: number): Promise<StoredReaderLibrary> {
  if (!UUID.test(chapterId) || !Number.isSafeInteger(pageIndex) || pageIndex < 0 || pageIndex > 9999) throw new RangeError("Marcador no válido");
  await loadReaderLibrary();
  return write(`/reader/library/bookmarks/${chapterId}/${pageIndex}`, "DELETE");
}
export function readerLibraryError(error: unknown): string {
  return error instanceof Error ? error.message : "No se pudieron guardar los datos del lector.";
}
