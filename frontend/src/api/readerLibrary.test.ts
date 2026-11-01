
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const libraryId = "123e4567-e89b-42d3-a456-426614174010";
const seriesId = "123e4567-e89b-42d3-a456-426614174050";
const chapterId = "123e4567-e89b-42d3-a456-426614174051";
const profile = { seriesId, format: "manga", mode: "single", rtl: true, continuous: false };
const marker = { seriesId, chapterId, pageIndex: 2, chapterLabel: "Capítulo 1", createdAt: "2026-10-25T12:00:00.000Z" };
const legacyKey = "yomira.reader.library.v1";
const envelope = (preferences: unknown[] = [], bookmarks: unknown[] = []) => ({ data: { library_id: libraryId, preferences, bookmarks }, meta: {} });
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
beforeEach(() => { localStorage.clear(); vi.resetModules(); vi.stubGlobal("crypto", { subtle: { digest: vi.fn(async () => new Uint8Array(32).fill(7).buffer) } }); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

describe("durable reader library client", () => {
  it("imports the exact legacy snapshot once and preserves its original copy", async () => {
    const raw = JSON.stringify({ preferences: [profile], bookmarks: [marker] });
    localStorage.setItem(legacyKey, raw);
    const server = envelope([{ series_id: seriesId, format: "manga", mode: "single", rtl: true, continuous: false }],
      [{ series_id: seriesId, chapter_id: chapterId, page_index: 2, chapter_label: marker.chapterLabel, created_at: marker.createdAt }]);
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => response(init?.method === "POST" ? server : envelope()));
    vi.stubGlobal("fetch", fetchMock);
    const api = await import("./readerLibrary");
    const result = await api.loadReaderLibrary();
    expect(result.bookmarks).toEqual([marker]);
    const body = JSON.parse(String(fetchMock.mock.calls.find(([, init]) => init?.method === "POST")?.[1]?.body));
    expect(body.migration_id).toMatch(/^[a-f0-9]{64}$/);
    expect(body.preferences[0].series_id).toBe(seriesId);
    expect(localStorage.getItem(legacyKey)).toBe(raw);
    await api.loadReaderLibrary();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("retries a failed import without deleting data or writing a receipt", async () => {
    const raw = JSON.stringify({ preferences: [profile], bookmarks: [marker] });
    localStorage.setItem(legacyKey, raw);
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === "POST"
      ? response({ error: { code: "API_REQUEST_REJECTED", message: "Biblioteca distinta", retryable: false, trace_id: "trace" } }, 409) : response(envelope())));
    const api = await import("./readerLibrary");
    await expect(api.loadReaderLibrary()).rejects.toThrow("La copia del navegador se conserva");
    expect(localStorage.getItem(legacyKey)).toBe(raw);
    expect(Object.keys(localStorage)).toEqual([legacyKey]);
  });

  it("keeps the migration identifier stable when receipt storage is blocked", async () => {
    localStorage.setItem(legacyKey, JSON.stringify({ preferences: [], bookmarks: [marker] }));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Full"); });
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>().mockImplementation(async () => response(envelope()));
    vi.stubGlobal("fetch", fetchMock);
    const api = await import("./readerLibrary");
    await api.loadReaderLibrary(); await api.loadReaderLibrary();
    const imports = fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/import"));
    expect(imports).toHaveLength(2);
    expect(JSON.parse(String(imports[0]?.[1]?.body)).migration_id).toBe(JSON.parse(String(imports[1]?.[1]?.body)).migration_id);
  });

  it("loads server data in another browser without a local copy", async () => {
    const fetchMock = vi.fn(async () => response(envelope([{ series_id: seriesId, format: "comic", mode: "double", rtl: false, continuous: false }])));
    vi.stubGlobal("fetch", fetchMock);
    const api = await import("./readerLibrary");
    expect((await api.loadReaderLibrary()).preferences[0]).toMatchObject({ format: "comic", mode: "double" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("preserves corrupt browser data and does not import it", async () => {
    localStorage.setItem(legacyKey, "corrupt");
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>().mockImplementation(async () => response(envelope()));
    vi.stubGlobal("fetch", fetchMock);
    const api = await import("./readerLibrary");
    expect((await api.loadReaderLibrary()).migrationWarning).toMatch(/conserva intacta/);
    expect(localStorage.getItem(legacyKey)).toBe("corrupt");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("rejects an invalid API response without accepting or archiving it", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(envelope([], [{ chapter_id: "bad" }]))));
    const api = await import("./readerLibrary");
    await expect(api.loadReaderLibrary()).rejects.toThrow("no válidos");
  });
});
