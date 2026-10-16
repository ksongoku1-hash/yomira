import { afterEach, describe, expect, it, vi } from "vitest";
import { getReaderDownloadSource, queueConnectorChapters } from "./client";

const chapterId = "123e4567-e89b-42d3-a456-426614174051";
function response(data: unknown, status = 200): Response {
  return { ok: status === 200, status, headers: { get: () => null },
    json: async () => data } as unknown as Response;
}
function jobs(ids: string[]) {
  const uuid = (prefix: string, number: number) => `${prefix}-0000-4000-8000-${String(number).padStart(12, "0")}`;
  return { data: ids.map((ref) => {
    const number = Number(ref.split("-")[1]);
    return { job_id: uuid("11111111", number), chapter_source_id: uuid("22222222", number),
      series_id: uuid("33333333", 1), chapter_id: uuid("44444444", number), status: "QUEUED",
      priority: "NORMAL", series_title: "Fixture", chapter_label: ref, created: true,
      created_at: "2026-10-11T00:00:00Z", updated_at: "2026-10-11T00:00:00Z" };
  }), meta: { connector_id: "yomira.mock", remote_series_id: "fixture" } };
}
afterEach(() => vi.unstubAllGlobals());
describe("reader download contracts", () => {
  it("reads canonical source references and accepts a local chapter without a source", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response({ data: { chapter_id: chapterId,
      connector_id: "yomira.mock", remote_series_id: "fixture", chapter_remote_id: "chapter-1" }, meta: {} }))
      .mockResolvedValueOnce(response({ data: null, meta: {} }));
    vi.stubGlobal("fetch", fetch);
    await expect(getReaderDownloadSource(chapterId)).resolves.toEqual({ connectorId: "yomira.mock",
      remoteSeriesId: "fixture", chapterRemoteId: "chapter-1" });
    await expect(getReaderDownloadSource(chapterId)).resolves.toBeNull();
  });
  it("rejects references for a different chapter", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ data: { chapter_id: "other-chapter",
      connector_id: "yomira.mock", remote_series_id: "fixture", chapter_remote_id: "chapter-1" }, meta: {} })));
    await expect(getReaderDownloadSource(chapterId)).rejects.toThrow();
  });
  it("deduplicates 105 chapters into two bounded batches", async () => {
    const batches: string[][] = [];
    vi.stubGlobal("fetch", vi.fn((_input: RequestInfo | URL, init: RequestInit) => {
      const ids = (JSON.parse(init.body as string) as { chapter_remote_ids: string[] }).chapter_remote_ids;
      batches.push(ids);
      return Promise.resolve(response(jobs(ids)));
    }));
    const ids = Array.from({ length: 105 }, (_, index) => `chapter-${index + 1}`);
    await expect(queueConnectorChapters("yomira.mock", "fixture", [...ids, ids[0]!])).resolves.toBe(105);
    expect(batches.map((batch) => batch.length)).toEqual([100, 5]);
    expect(batches.flat()).toEqual(ids);
  });
  it("validates the entire selection before creating its first batch", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const ids = Array.from({ length: 100 }, (_, index) => `chapter-${index + 1}`);
    await expect(queueConnectorChapters("yomira.mock", "fixture", [...ids, "x".repeat(513)])).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
  it("stops after a failed batch and preserves the earlier successful requests", async () => {
    const batches: string[][] = [];
    const fetch = vi.fn((_input: RequestInfo | URL, init: RequestInit) => {
      const ids = (JSON.parse(init.body as string) as { chapter_remote_ids: string[] }).chapter_remote_ids;
      batches.push(ids);
      return Promise.resolve(batches.length === 1 ? response(jobs(ids)) : response({}, 503));
    });
    vi.stubGlobal("fetch", fetch);
    await expect(queueConnectorChapters("yomira.mock", "fixture",
      Array.from({ length: 205 }, (_, index) => `chapter-${index + 1}`))).rejects.toThrow();
    expect(batches.map((batch) => batch.length)).toEqual([100, 100]);
  });
});
