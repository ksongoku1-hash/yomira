import { afterEach, expect, it, vi } from "vitest";
import { ApiError, createSeriesEpub, getSeriesEpubFileUrl, getSeriesEpubStatus, prepareSeriesEpubUsbAsset } from "./client";

const seriesId = "123e4567-e89b-42d3-a456-426614174000";
const state = { status: "ready", filename: "Mi manga.epub", chapter_count: 103, page_count: 20_000,
  completed_pages: 20_000, size_bytes: 20 * 1024 ** 3, error: null, has_file: true };

function response(data: unknown, status = 200): Response {
  return { status, ok: status >= 200 && status < 300, headers: new Headers(), json: async () => ({ data }) } as Response;
}
afterEach(() => vi.unstubAllGlobals());

it("prepares a verified EPUB for USB and rejects an invalid asset response", async () => {
  const assetId = "123e4567-e89b-42d3-a456-426614174001";
  const fetch = vi.fn().mockResolvedValueOnce(response({ asset_id: assetId }))
    .mockResolvedValueOnce(response({ asset_id: "../untrusted" }));
  vi.stubGlobal("fetch", fetch);
  expect(await prepareSeriesEpubUsbAsset(seriesId)).toBe(assetId);
  expect(fetch).toHaveBeenCalledWith(`/api/v1/usb-exports/epub/${seriesId}/asset`, {
    method: "POST", headers: { Accept: "application/json" },
  });
  await expect(prepareSeriesEpubUsbAsset(seriesId)).rejects.toBeInstanceOf(ApiError);
  await expect(prepareSeriesEpubUsbAsset("invalid")).rejects.toBeInstanceOf(RangeError);
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("reads the whole manga state without imposing chapter-export size limits", async () => {
  const fetch = vi.fn().mockResolvedValue(response(state)); vi.stubGlobal("fetch", fetch);
  expect(await getSeriesEpubStatus(seriesId)).toEqual({ status: "ready", filename: "Mi manga.epub", chapterCount: 103,
    pageCount: 20_000, completedPages: 20_000, sizeBytes: 20 * 1024 ** 3, error: null, hasFile: true });
  expect(fetch).toHaveBeenCalledWith(`/api/v1/series/${seriesId}/epub`, { headers: { Accept: "application/json" } });
  expect(getSeriesEpubFileUrl(seriesId)).toBe(`/api/v1/series/${seriesId}/epub/file`);
});

it("queues one book for the series with POST and no list, body or selected-page parameters", async () => {
  const fetch = vi.fn().mockResolvedValue(response({ ...state, status: "building", completed_pages: 0 }, 202));
  vi.stubGlobal("fetch", fetch);
  expect((await createSeriesEpub(seriesId)).status).toBe("building");
  expect(fetch).toHaveBeenCalledWith(`/api/v1/series/${seriesId}/epub`, { method: "POST", headers: { Accept: "application/json" } });
});

it.each(["not_created", "building", "ready", "outdated", "failed"])("accepts the documented state %s", async status => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ ...state, status })));
  expect((await getSeriesEpubStatus(seriesId)).status).toBe(status);
});

it.each([
  { status: "unknown" }, { chapter_count: -1 }, { page_count: 1.2 }, { completed_pages: 20_001 },
  { completed_pages: -1 }, { page_count: NaN }, { size_bytes: Infinity }, { size_bytes: Number.MAX_SAFE_INTEGER + 1 },
  { has_file: "true" }, { filename: "../other.epub" }, { filename: "bad\nname.epub" }, { filename: "Manga.zip" },
])("rejects invalid EPUB metadata without returning it to the view: %o", async patch => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ ...state, ...patch })));
  await expect(getSeriesEpubStatus(seriesId)).rejects.toMatchObject({ name: "ApiError", message: "No se pudo leer el estado del EPUB." });
});

it("preserves an existing file in a failed update", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ ...state, status: "failed", error: "No hay espacio suficiente." })));
  expect(await getSeriesEpubStatus(seriesId)).toMatchObject({ status: "failed", hasFile: true, filename: "Mi manga.epub" });
});

it("refuses invalid identifiers before issuing a request", async () => {
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  await expect(getSeriesEpubStatus("../other")).rejects.toBeInstanceOf(RangeError);
  await expect(createSeriesEpub("../other")).rejects.toBeInstanceOf(RangeError);
  expect(() => getSeriesEpubFileUrl("../other")).toThrow(RangeError);
  expect(fetch).not.toHaveBeenCalled();
});

it("requires the accepted POST response and reports a readable failure", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(state, 200)));
  await expect(createSeriesEpub(seriesId)).rejects.toBeInstanceOf(ApiError);
});
