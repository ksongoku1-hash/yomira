import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  applyExternalSeriesMetadata,
  clearMetadataOverride,
  cancelDownloadJob,
  createConnectorDownloadJobs,
  getConnectorChapters,
  getConnectorCover,
  getConnectorSearch,
  getChapterAvailability,
  getDownloadJobsPage,
  getDownloadGroupsPage,
  setDownloadGroupPaused,
  getConnectors,
  getLibraryPage,
  getLocalImportStatus,
  getRecentLibrary,
  getRecentReaderProgress,
  getReaderChapter,
  openOnlineReaderChapter,
  registerOnlineReaderChapter,
  getReaderChaptersPage,
  getReaderPageBlob,
  getReaderProgress,
  getReaderSettings,
  getSeriesDetail,
  getExternalMetadataCandidates,
  getSeriesCoverUrl,
  getSystemDiagnostics,
  getSystemLiveness,
  getSystemReadiness,
  getSystemVersion,
  importConnectorLibrarySeries,
  putReaderProgress,
  putReaderSettings,
  refreshSeriesMetadata,
  setMetadataOverride,
} from "./client";

function mockResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => payload,
  } as unknown as Response;
}

function mockBinaryResponse({
  status = 200,
  contentType = "image/jpeg",
  contentLength = "3",
  payload = new Blob([new Uint8Array([1, 2, 3])], { type: contentType }),
}: {
  status?: number;
  contentType?: string;
  contentLength?: string | null;
  payload?: Blob;
} = {}): Response {
  const headers: Record<string, string> = {};
  if (contentType) headers["Content-Type"] = contentType;
  if (contentLength !== null) headers["Content-Length"] = contentLength;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => headers[name] ?? null },
    json: async () => ({
      error: {
        code: "READ_ASSET_NOT_FOUND",
        message: "La página no está disponible.",
        retryable: false,
        trace_id: "trace-reader-page",
      },
    }),
    blob: async () => payload,
    body: null,
  } as unknown as Response;
}

function series(id: string, displayTitle: string) {
  const emptyMetadata = (field: string, value: string | null, authority: string | null) => ({
    field,
    value,
    authority,
    local_value: value,
    source_values: [],
    has_user_override: false,
    user_override_value: null,
    source_conflict: false,
    effective_source_ids: [],
  });

  return {
    series_id: id,
    canonical_title: displayTitle.toLowerCase(),
    display_title: displayTitle,
    description: null,
    status: null,
    language: "ja",
    cover_asset_id: null,
    created_at: "2026-09-26T10:00:00Z",
    updated_at: "2026-09-26T10:00:00Z",
    metadata: [
      emptyMetadata("title", displayTitle, "LOCAL_CANONICAL"),
      emptyMetadata("cover", null, null),
      emptyMetadata("author", null, null),
      emptyMetadata("description", null, null),
      emptyMetadata("genres", null, null),
      emptyMetadata("status", null, null),
      emptyMetadata("language", "ja", "LOCAL_CANONICAL"),
    ],
  };
}

describe("typed local API client", () => {
  it("validates download groups and sends an explicit pause state", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    const group = {series_id: seriesId, series_title: "Manga", queued: 2, active: 1, paused: true};
    const fetchMock = vi.fn().mockResolvedValue(mockResponse({data: [group], meta: {limit: 50, offset: 0, total: 1}}));
    vi.stubGlobal("fetch", fetchMock);
    expect((await getDownloadGroupsPage()).data[0]).toEqual({seriesId, seriesTitle: "Manga", queued: 2, active: 1, paused: true});
    for (const invalid of [{...group, queued: -1}, {...group, paused: "true"}, {...group, series_id: "bad-id"}]) {
      fetchMock.mockResolvedValueOnce(mockResponse({data: [invalid], meta: {limit: 50, offset: 0, total: 1}}));
      await expect(getDownloadGroupsPage()).rejects.toThrow();
    }
    fetchMock.mockResolvedValueOnce(mockResponse({data: [group, group], meta: {limit: 50, offset: 0, total: 2}}));
    await expect(getDownloadGroupsPage()).rejects.toThrow();
    fetchMock.mockResolvedValueOnce(mockResponse(null, 204));
    await setDownloadGroupPaused(seriesId, false);
    expect(fetchMock).toHaveBeenLastCalledWith(`/api/v1/download-jobs/groups/${seriesId}/pause`, expect.objectContaining({method: "PUT", body: '{"paused":false}'}));
  });
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([undefined, "11111111-1111-4111-8111-111111111111", "../other-path"])(
    "validates the optional readable copy in import warnings: %s", async (chapterId) => {
      fetchMock.mockResolvedValue(mockResponse({ folder: "C:/Mangas", scanning: false,
        imported_chapters: 0, pending_metadata: 0,
        errors: [{ file: "Chapter.epub", message: "Incomplete image", chapter_id: chapterId }] }));
      if (chapterId === "../other-path") {
        await expect(getLocalImportStatus()).rejects.toThrow();
      } else {
        expect((await getLocalImportStatus()).errors[0]?.chapterId).toBe(chapterId);
      }
    });

  it("lists the registered connector catalog with exact fields and capabilities", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [
          {
            connector_id: "yomira.mock",
            connector_version: "1.0.0",
            capabilities: ["SEARCH", "CHAPTER_LIST", "PAGE_LIST"],
          },
          {
            connector_id: "yomira.fmd2.webtoons",
            connector_version: "2.1.0",
            capabilities: ["SEARCH", "METADATA", "CHAPTER_LIST"],
          },
        ],
      }),
    );

    await expect(getConnectors()).resolves.toEqual({
      data: [
        {
          connectorId: "yomira.mock",
          connectorVersion: "1.0.0",
          capabilities: ["SEARCH", "CHAPTER_LIST", "PAGE_LIST"],
        },
        {
          connectorId: "yomira.fmd2.webtoons",
          connectorVersion: "2.1.0",
          capabilities: ["SEARCH", "METADATA", "CHAPTER_LIST"],
        },
      ],
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/connectors", {
      headers: { Accept: "application/json" },
    });
  });

  it("rejects duplicate connectors, unknown capabilities, and extra descriptor fields", async () => {
    const descriptor = {
      connector_id: "yomira.mock",
      connector_version: "1.0.0",
      capabilities: ["SEARCH"],
    };
    for (const data of [
      [descriptor, descriptor],
      [{ ...descriptor, capabilities: ["FUTURE_CAPABILITY"] }],
      [{ ...descriptor, enabled: true }],
    ]) {
      fetchMock.mockResolvedValueOnce(mockResponse({ data }));
      await expect(getConnectors()).rejects.toMatchObject({
        name: "ApiError",
        message: "La API devolvió una lista de conectores no válida.",
      });
    }
  });

  it("searches a selected connector with an explicitly encoded query", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [
          {
            remote_id: "series/one",
            remote_url: "https://example.test/series/one",
            remote_title: "Mock Series",
            cover_url: "https://example.test/covers/one.webp",
            source_label: "Extension Name (en)",
          },
        ],
        meta: { connector_id: "yomira.mock", query: "House of the Dragon" },
      }),
    );

    await expect(getConnectorSearch("yomira.mock", "House of the Dragon")).resolves.toEqual({
      data: [
        {
          remoteId: "series/one",
          remoteUrl: "https://example.test/series/one",
          remoteTitle: "Mock Series",
          coverUrl: "https://example.test/covers/one.webp",
          sourceLabel: "Extension Name (en)",
        },
      ],
      meta: { connectorId: "yomira.mock", query: "House of the Dragon" },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.mock/search?query=House+of+the+Dragon",
      { headers: { Accept: "application/json" } },
    );
  });

  it("supports an explicit bounded page expansion and forwards query cancellation", async () => {
    const controller = new AbortController();
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [],
        meta: { connector_id: "yomira.fmd2.webtoons", query: "Mato Seihei" },
      }),
    );

    await expect(
      getConnectorSearch("yomira.fmd2.webtoons", "Mato Seihei", 20, controller.signal),
    ).resolves.toEqual({
      data: [],
      meta: { connectorId: "yomira.fmd2.webtoons", query: "Mato Seihei" },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.fmd2.webtoons/search?query=Mato+Seihei&page_budget=20",
      { headers: { Accept: "application/json" }, signal: controller.signal },
    );
  });

  it("rejects invalid search page budgets before making a request", async () => {
    await expect(getConnectorSearch("yomira.fmd2.webtoons", "Mato", 21))
      .rejects.toThrow("pageBudget must be an integer from 1 to 20");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("preserves AbortError when a connector search is cancelled", async () => {
    const controller = new AbortController();
    let receivedSignal: AbortSignal | undefined;
    fetchMock.mockImplementation((_input, init) => new Promise((_resolve, reject) => {
      receivedSignal = init?.signal ?? undefined;
      receivedSignal?.addEventListener(
        "abort",
        () => reject(new DOMException("The request was cancelled.", "AbortError")),
        { once: true },
      );
    }));

    const request = getConnectorSearch("yomira.fmd2.webtoons", "Mato Seihei", 20, controller.signal);
    controller.abort();

    await expect(request).rejects.toMatchObject({ name: "AbortError" });
    expect(receivedSignal).toBe(controller.signal);
  });

  it("preserves optional remote identities and chapter labels", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [
          {
            remote_id: "chapter-1",
            display_label: "Capítulo 1",
            remote_label: "01",
            chapter_raw_label: "01",
            chapter_sort_key: [1, 0],
            language: "es",
          },
          {
            remote_id: null,
            display_label: "Extra",
            remote_label: null,
            chapter_raw_label: "Extra",
            chapter_sort_key: [],
            language: null,
          },
        ],
        meta: { connector_id: "yomira.mock", remote_series_id: "series/one" },
      }),
    );

    await expect(getConnectorChapters("yomira.mock", "series/one")).resolves.toEqual({
      data: [
        {
          remoteId: "chapter-1",
          displayLabel: "Capítulo 1",
          remoteLabel: "01",
          chapterRawLabel: "01",
          chapterSortKey: [1, 0],
          language: "es",
        },
        {
          remoteId: null,
          displayLabel: "Extra",
          remoteLabel: null,
          chapterRawLabel: "Extra",
          chapterSortKey: [],
          language: null,
        },
      ],
      meta: { connectorId: "yomira.mock", remoteSeriesId: "series/one" },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.mock/series/series%2Fone/chapters",
      { headers: { Accept: "application/json" } },
    );
  });

  it("gets a source cover using the metadata connector contract", async () => {
    fetchMock.mockResolvedValue(mockResponse({
      data: { cover_url: "https://zonatmo.org/covers/dragon-ball.jpg" },
      meta: { connector_id: "yomira.fmd2.webtoons", remote_series_id: "fmd2:v1:abc" },
    }));

    await expect(getConnectorCover("yomira.fmd2.webtoons", "fmd2:v1:abc")).resolves.toEqual({
      data: { coverUrl: "https://zonatmo.org/covers/dragon-ball.jpg" },
      meta: { connectorId: "yomira.fmd2.webtoons", remoteSeriesId: "fmd2:v1:abc" },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.fmd2.webtoons/series/fmd2%3Av1%3Aabc/cover",
      { headers: { Accept: "application/json" } },
    );
    await getConnectorCover("yomira.fmd2.webtoons", "fmd2:v1:abc", "Dragon Ball & Other");
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/connectors/yomira.fmd2.webtoons/series/fmd2%3Av1%3Aabc/cover?title=Dragon%20Ball%20%26%20Other",
      { headers: { Accept: "application/json" } },
    );
    await expect(getConnectorCover("yomira.fmd2.webtoons", "fmd2:v1:abc", "x".repeat(201)))
      .rejects.toThrow(RangeError);
  });

  it("rejects connector responses whose echoed request metadata does not match", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [],
        meta: { connector_id: "another.connector", query: "different query" },
      }),
    );

    await expect(getConnectorSearch("yomira.mock", "expected query")).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      retryable: false,
      message: "La API devolvió una respuesta de búsqueda no válida.",
    });
  });

  it("rejects malformed chapter sort keys instead of presenting partial data", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [
          {
            remote_id: "chapter-1",
            display_label: "Capítulo 1",
            remote_label: "1",
            chapter_raw_label: "1",
            chapter_sort_key: [1, 1.5],
            language: "es",
          },
        ],
        meta: { connector_id: "yomira.mock", remote_series_id: "series-1" },
      }),
    );

    await expect(getConnectorChapters("yomira.mock", "series-1")).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      retryable: false,
      message: "La API devolvió una respuesta de capítulos no válida.",
    });
  });

  it("imports a connector series into the library without creating download jobs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockResponse({
      data: {
        series_id: "123e4567-e89b-42d3-a456-426614174050",
        series_title: "Fate Zero",
        created: true,
      },
      meta: { connector_id: "yomira.fmd2.webtoons", remote_series_id: "/manga/fate-zero" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(importConnectorLibrarySeries("yomira.fmd2.webtoons", "/manga/fate-zero"))
      .resolves.toEqual({
        data: {
          seriesId: "123e4567-e89b-42d3-a456-426614174050",
          seriesTitle: "Fate Zero",
          created: true,
        },
        meta: { connectorId: "yomira.fmd2.webtoons", remoteSeriesId: "/manga/fate-zero" },
      });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.fmd2.webtoons/series/%2Fmanga%2Ffate-zero/library-series",
      { method: "POST", headers: { Accept: "application/json" } },
    );
  });

  it("rejects invalid library imports and mismatched source metadata", async () => {
    const data = {
      series_id: "123e4567-e89b-42d3-a456-426614174050",
      series_title: "Fate Zero",
      created: true,
    };
    const meta = { connector_id: "yomira.fmd2.webtoons", remote_series_id: "/manga/fate-zero" };
    const invalidPayloads = [
      { data: { ...data, series_id: "not-a-uuid" }, meta },
      { data: { ...data, series_title: " " }, meta },
      { data: { ...data, created: "true" }, meta },
      { data: { ...data, unexpected: "field" }, meta },
      { data, meta: { ...meta, connector_id: "another.source" } },
      { data, meta: { ...meta, remote_series_id: "/manga/another-series" } },
      { data, meta: { ...meta, unexpected: "field" } },
      { data, meta, unexpected: "field" },
    ];
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    for (const payload of invalidPayloads) {
      fetchMock.mockResolvedValueOnce(mockResponse(payload));
      await expect(importConnectorLibrarySeries("yomira.fmd2.webtoons", "/manga/fate-zero"))
        .rejects.toMatchObject({ retryable: false, status: 200 });
    }
  });

  it("validates library import references and preserves source failure details", async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockResponse({
      error: {
        code: "SRC_CONNECTOR_FAILED",
        message: "La fuente no está disponible.",
        retryable: false,
        trace_id: "trace-import-source",
      },
    }, 502));
    vi.stubGlobal("fetch", fetchMock);
    await expect(importConnectorLibrarySeries("", "series-1")).rejects.toThrow(RangeError);
    await expect(importConnectorLibrarySeries("yomira.fmd2.webtoons", ""))
      .rejects.toThrow(RangeError);
    expect(fetchMock).not.toHaveBeenCalled();
    await expect(importConnectorLibrarySeries("yomira.fmd2.webtoons", "/manga/fate-zero"))
      .rejects.toMatchObject({
        code: "SRC_CONNECTOR_FAILED", status: 502, traceId: "trace-import-source", retryable: false,
      });
  });

  it("creates selected connector jobs with validated response metadata", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [
          {
            job_id: "123e4567-e89b-42d3-a456-426614174000",
            chapter_source_id: "123e4567-e89b-42d3-a456-426614174001",
            series_id: "123e4567-e89b-42d3-a456-426614174050",
            chapter_id: "123e4567-e89b-42d3-a456-426614174051",
            status: "QUEUED",
            priority: "LOW",
            series_title: "Mock Fixture Series",
            chapter_label: "Capítulo 1",
            created: true,
            created_at: "2026-09-27T10:00:00Z",
            updated_at: "2026-09-27T10:00:00Z",
          },
        ],
        meta: { connector_id: "yomira.mock", remote_series_id: "fixture/series" },
      }),
    );

    await expect(
      createConnectorDownloadJobs("yomira.mock", "fixture/series", {
        chapterRemoteIds: ["chapter-1"],
        priority: "LOW",
      }),
    ).resolves.toEqual({
      data: [
        {
          jobId: "123e4567-e89b-42d3-a456-426614174000",
          chapterSourceId: "123e4567-e89b-42d3-a456-426614174001",
          seriesId: "123e4567-e89b-42d3-a456-426614174050",
          chapterId: "123e4567-e89b-42d3-a456-426614174051",
          status: "QUEUED",
          priority: "LOW",
          seriesTitle: "Mock Fixture Series",
          chapterLabel: "Capítulo 1",
          created: true,
          cancelRequestedAt: null,
          createdAt: "2026-09-27T10:00:00Z",
          updatedAt: "2026-09-27T10:00:00Z",
        },
      ],
      meta: { connectorId: "yomira.mock", remoteSeriesId: "fixture/series" },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.mock/series/fixture%2Fseries/download-jobs",
      {
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        method: "POST",
        body: JSON.stringify({ chapter_remote_ids: ["chapter-1"], priority: "LOW" }),
      },
    );
  });

  it("rejects invalid or incomplete created-job records as a whole", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [
          {
            job_id: "123e4567-e89b-42d3-a456-426614174000",
            chapter_source_id: "123e4567-e89b-42d3-a456-426614174001",
            series_id: "123e4567-e89b-42d3-a456-426614174050",
            chapter_id: "123e4567-e89b-42d3-a456-426614174051",
            status: "DOWNLOADED",
            priority: "NORMAL",
            series_title: "Mock Fixture Series",
            chapter_label: "Capítulo 1",
            created: true,
            created_at: "2026-09-27T10:00:00Z",
            updated_at: "2026-09-27T10:00:00Z",
          },
        ],
        meta: { connector_id: "yomira.mock", remote_series_id: "fixture/series" },
      }),
    );

    await expect(
      createConnectorDownloadJobs("yomira.mock", "fixture/series", {
        chapterRemoteIds: ["chapter-1"],
      }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      message: "La API devolvió una respuesta de trabajos de descarga no válida.",
    });
  });

  it("refuses invalid selected IDs before sending a create request", async () => {
    await expect(
      createConnectorDownloadJobs("yomira.mock", "series-1", {
        chapterRemoteIds: ["same-id", "same-id"],
      }),
    ).rejects.toBeInstanceOf(RangeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects inconsistent totals and validates chapter availability", async () => {
    fetchMock.mockResolvedValue(mockResponse({
      data: [], meta: { limit: 50, offset: 0, total: 2, states: { QUEUED: 1 } },
    }));
    await expect(getDownloadJobsPage({ limit: 50, offset: 0 })).rejects.toMatchObject({
      name: "ApiError",
    });
    fetchMock.mockResolvedValue(mockResponse({ data: {
      linked: true, ready_remote_ids: ["chapter-1"], ready_labels: [],
      busy_remote_ids: ["chapter-2"],
    } }));
    await expect(getChapterAvailability("yomira.mock", "edition/one")).resolves.toEqual({
      linked: true, readyRemoteIds: ["chapter-1"], readyLabels: [], busyRemoteIds: ["chapter-2"],
    });
    fetchMock.mockResolvedValue(mockResponse({ data: {
      linked: true, ready_remote_ids: [42], ready_labels: [], busy_remote_ids: [],
    } }));
    await expect(getChapterAvailability("yomira.mock", "edition/one")).rejects.toMatchObject({
      name: "ApiError",
    });
  });

  it("lists bounded download jobs and validates the exact page contract", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [
          {
            job_id: "123e4567-e89b-42d3-a456-426614174000",
            chapter_source_id: "123e4567-e89b-42d3-a456-426614174001",
            series_id: "123e4567-e89b-42d3-a456-426614174050",
            chapter_id: "123e4567-e89b-42d3-a456-426614174051",
            status: "QUEUED",
            priority: "NORMAL",
            series_title: "Mock Fixture Series",
            chapter_label: "Capítulo 1",
            created_at: "2026-09-27T10:00:00Z",
            updated_at: "2026-09-27T10:00:00Z",
            cancel_requested_at: null,
          },
        ],
        meta: { limit: 50, offset: 0, total: 1 },
      }),
    );

    await expect(getDownloadJobsPage({ limit: 50, offset: 0 })).resolves.toEqual({
      data: [
        {
          jobId: "123e4567-e89b-42d3-a456-426614174000",
          chapterSourceId: "123e4567-e89b-42d3-a456-426614174001",
          seriesId: "123e4567-e89b-42d3-a456-426614174050",
          chapterId: "123e4567-e89b-42d3-a456-426614174051",
          status: "QUEUED",
          priority: "NORMAL",
          seriesTitle: "Mock Fixture Series",
          chapterLabel: "Capítulo 1",
          createdAt: "2026-09-27T10:00:00Z",
          updatedAt: "2026-09-27T10:00:00Z",
          cancelRequestedAt: null,
        },
      ],
      meta: { limit: 50, offset: 0, total: 1 },
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/download-jobs?limit=50&offset=0", {
      headers: { Accept: "application/json" },
    });

    fetchMock.mockResolvedValue(
      mockResponse({ data: [], meta: { limit: 25, offset: 0, total: 0 } }),
    );
    await expect(getDownloadJobsPage({ limit: 50, offset: 0 })).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      retryable: false,
    });
  });

  it("requests durable job cancellation and validates the canonical response", async () => {
    const jobId = "123e4567-e89b-42d3-a456-426614174000";
    fetchMock.mockResolvedValue(
      mockResponse({
        data: {
          job_id: jobId,
          chapter_source_id: "123e4567-e89b-42d3-a456-426614174001",
          series_id: "123e4567-e89b-42d3-a456-426614174050",
          chapter_id: "123e4567-e89b-42d3-a456-426614174051",
          status: "DOWNLOADING",
          priority: "NORMAL",
          series_title: "Mock Fixture Series",
          chapter_label: "Capítulo 1",
          created_at: "2026-09-27T10:00:00Z",
          updated_at: "2026-09-27T10:05:00Z",
          cancel_requested_at: "2026-09-27T10:06:00Z",
        },
      }),
    );

    await expect(cancelDownloadJob(jobId)).resolves.toEqual({
      jobId,
      chapterSourceId: "123e4567-e89b-42d3-a456-426614174001",
      seriesId: "123e4567-e89b-42d3-a456-426614174050",
      chapterId: "123e4567-e89b-42d3-a456-426614174051",
      status: "DOWNLOADING",
      priority: "NORMAL",
      seriesTitle: "Mock Fixture Series",
      chapterLabel: "Capítulo 1",
      createdAt: "2026-09-27T10:00:00Z",
      updatedAt: "2026-09-27T10:05:00Z",
      cancelRequestedAt: "2026-09-27T10:06:00Z",
    });
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/downloads/${jobId}/cancel`, {
      headers: { Accept: "application/json" },
      method: "POST",
    });
  });

  it("accepts an idempotent cancellation response for a historical cancelled job", async () => {
    const jobId = "123e4567-e89b-42d3-a456-426614174000";
    fetchMock.mockResolvedValue(
      mockResponse({
        data: {
          job_id: jobId,
          chapter_source_id: "123e4567-e89b-42d3-a456-426614174001",
          series_id: "123e4567-e89b-42d3-a456-426614174050",
          chapter_id: "123e4567-e89b-42d3-a456-426614174051",
          status: "CANCELLED",
          priority: "NORMAL",
          series_title: "Mock Fixture Series",
          chapter_label: "Capítulo 1",
          created_at: "2026-09-27T10:00:00Z",
          updated_at: "2026-09-27T10:05:00Z",
          cancel_requested_at: null,
        },
      }),
    );

    await expect(cancelDownloadJob(jobId)).resolves.toMatchObject({
      jobId,
      status: "CANCELLED",
      cancelRequestedAt: null,
    });
  });

  it.each(["FAILED", "INTERRUPTED"] as const)(
    "accepts a durable cancellation intent when the worker ends as %s",
    async (status) => {
      const jobId = "123e4567-e89b-42d3-a456-426614174000";
      const cancelRequestedAt = "2026-09-27T10:06:00Z";
      fetchMock.mockResolvedValue(
        mockResponse({
          data: {
            job_id: jobId,
            chapter_source_id: "123e4567-e89b-42d3-a456-426614174001",
            series_id: "123e4567-e89b-42d3-a456-426614174050",
            chapter_id: "123e4567-e89b-42d3-a456-426614174051",
            status,
            priority: "NORMAL",
            series_title: "Mock Fixture Series",
            chapter_label: "Capítulo 1",
            created_at: "2026-09-27T10:00:00Z",
            updated_at: "2026-09-27T10:05:00Z",
            cancel_requested_at: cancelRequestedAt,
          },
        }),
      );

      await expect(cancelDownloadJob(jobId)).resolves.toMatchObject({
        jobId,
        status,
        cancelRequestedAt,
      });
    },
  );

  it("accepts historical retry jobs that share one chapter source", async () => {
    const sharedChapterSourceId = "123e4567-e89b-42d3-a456-426614174001";
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [
          {
            job_id: "123e4567-e89b-42d3-a456-426614174000",
            chapter_source_id: sharedChapterSourceId,
            series_id: "123e4567-e89b-42d3-a456-426614174050",
            chapter_id: "123e4567-e89b-42d3-a456-426614174051",
            status: "FAILED",
            priority: "NORMAL",
            series_title: "Mock Fixture Series",
            chapter_label: "Capítulo 1",
            created_at: "2026-09-27T10:00:00Z",
            updated_at: "2026-09-27T10:05:00Z",
            cancel_requested_at: null,
          },
          {
            job_id: "123e4567-e89b-42d3-a456-426614174002",
            chapter_source_id: sharedChapterSourceId,
            series_id: "123e4567-e89b-42d3-a456-426614174050",
            chapter_id: "123e4567-e89b-42d3-a456-426614174051",
            status: "QUEUED",
            priority: "NORMAL",
            series_title: "Mock Fixture Series",
            chapter_label: "Capítulo 1",
            created_at: "2026-09-27T10:06:00Z",
            updated_at: "2026-09-27T10:06:00Z",
            cancel_requested_at: null,
          },
        ],
        meta: { limit: 50, offset: 0, total: 2 },
      }),
    );

    await expect(getDownloadJobsPage({ limit: 50, offset: 0 })).resolves.toMatchObject({
      data: [
        {
          jobId: "123e4567-e89b-42d3-a456-426614174000",
          chapterSourceId: sharedChapterSourceId,
          status: "FAILED",
        },
        {
          jobId: "123e4567-e89b-42d3-a456-426614174002",
          chapterSourceId: sharedChapterSourceId,
          status: "QUEUED",
        },
      ],
      meta: { limit: 50, offset: 0, total: 2 },
    });
  });

  it("requests the relative API path and validates liveness", async () => {
    fetchMock.mockResolvedValue(mockResponse({ status: "alive" }));

    await expect(getSystemLiveness()).resolves.toEqual({ status: "alive" });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/system/liveness", {
      headers: { Accept: "application/json" },
    });
  });

  it("treats HTTP 503 with not_ready as a valid readiness state only", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ status: "ready" }));
    await expect(getSystemReadiness()).resolves.toEqual({ status: "ready" });

    fetchMock.mockResolvedValueOnce(mockResponse({ status: "not_ready" }, 503));
    await expect(getSystemReadiness()).resolves.toEqual({ status: "not_ready" });
    expect(fetchMock).toHaveBeenLastCalledWith("/api/v1/system/readiness", {
      headers: { Accept: "application/json" },
    });

    fetchMock.mockResolvedValueOnce(mockResponse({ status: "not_ready" }, 200));
    await expect(getSystemReadiness()).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      retryable: false,
    });

    fetchMock.mockResolvedValueOnce(mockResponse({ status: "ready" }, 201));
    await expect(getSystemReadiness()).rejects.toMatchObject({
      name: "ApiError",
      status: 201,
      retryable: false,
    });
  });

  it("loads system diagnostics and preserves unknown as an explicit status", async () => {
    const ids = [
      "backend",
      "database",
      "library",
      "disk",
      "image_processor",
      "cbz_builder",
      "connectors",
      "lan",
    ];
    fetchMock.mockResolvedValue(mockResponse({
      app_version: "1.2.3",
      api_version: 1,
      schema_version: "0019_usb_export_history",
      checks: ids.map((id) => ({
        id,
        status: id === "disk" ? "unknown" : "ok",
        code: null,
        message: id === "disk" ? "No se pudo consultar el espacio disponible." : "Disponible.",
        trace_id: null,
      })),
    }));

    const diagnostics = await getSystemDiagnostics();
    expect(diagnostics).toMatchObject({
      appVersion: "1.2.3",
      apiVersion: 1,
      schemaVersion: "0019_usb_export_history",
    });
    expect(diagnostics.checks).toHaveLength(8);
    expect(diagnostics.checks.find((check) => check.id === "disk")).toEqual({
      id: "disk",
      status: "unknown",
      code: null,
      message: "No se pudo consultar el espacio disponible.",
      traceId: null,
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/system/diagnostics", {
      headers: { Accept: "application/json" },
    });
  });

  it("rejects malformed diagnostic statuses and preserves traced API errors", async () => {
    const ids = [
      "backend",
      "database",
      "library",
      "disk",
      "image_processor",
      "cbz_builder",
      "connectors",
      "lan",
    ];
    fetchMock.mockResolvedValueOnce(mockResponse({
      app_version: "1.2.3",
      api_version: 1,
      schema_version: "0019_usb_export_history",
      checks: ids.map((id) => ({
        id,
        status: id === "disk" ? "healthy" : "unknown",
        code: null,
        message: id === "disk" ? "This is not an approved status." : "No disponible.",
        trace_id: null,
      })),
    }));
    await expect(getSystemDiagnostics()).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      retryable: false,
    });

    fetchMock.mockResolvedValueOnce(mockResponse({
      error: {
        code: "SYS_DIAGNOSTICS_UNAVAILABLE",
        message: "No se pudo consultar el diagnóstico.",
        retryable: true,
        trace_id: "trace-diagnostics-client",
      },
    }, 503));
    await expect(getSystemDiagnostics()).rejects.toMatchObject({
      name: "ApiError",
      code: "SYS_DIAGNOSTICS_UNAVAILABLE",
      status: 503,
      retryable: true,
      traceId: "trace-diagnostics-client",
    });
  });

  it("rejects diagnostics that omit a required subsystem", async () => {
    const ids = [
      "backend",
      "database",
      "library",
      "disk",
      "image_processor",
      "cbz_builder",
      "connectors",
      "lan",
    ];
    fetchMock.mockResolvedValue(mockResponse({
      app_version: "1.2.3",
      api_version: 1,
      schema_version: "0019_usb_export_history",
      checks: ids.slice(1).map((id) => ({
        id,
        status: "unknown",
        code: null,
        message: "Comprobación no disponible.",
        trace_id: null,
      })),
    }));

    await expect(getSystemDiagnostics()).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      retryable: false,
      message: "La API devolvió una respuesta de diagnóstico no válida.",
    });
  });

  it("rejects duplicated diagnostic subsystem IDs", async () => {
    const ids = [
      "backend",
      "database",
      "library",
      "disk",
      "image_processor",
      "cbz_builder",
      "connectors",
      "lan",
    ];
    const duplicateIds = [...ids.slice(0, -1), "backend"];
    fetchMock.mockResolvedValue(mockResponse({
      app_version: "1.2.3",
      api_version: 1,
      schema_version: "0019_usb_export_history",
      checks: duplicateIds.map((id) => ({
        id,
        status: "unknown",
        code: null,
        message: "Comprobación no disponible.",
        trace_id: null,
      })),
    }));

    await expect(getSystemDiagnostics()).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      retryable: false,
      message: "La API devolvió una respuesta de diagnóstico no válida.",
    });
  });

  it("loads validated chapter metadata with 1-based page counts", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: {
          chapter_id: "123e4567-e89b-42d3-a456-426614174051",
          series_id: "123e4567-e89b-42d3-a456-426614174050",
          display_label: "Capítulo 8",
          page_count: 12,
        },
        meta: {},
      }),
    );

    await expect(
      getReaderChapter("123e4567-e89b-42d3-a456-426614174051"),
    ).resolves.toEqual({
      chapterId: "123e4567-e89b-42d3-a456-426614174051",
      seriesId: "123e4567-e89b-42d3-a456-426614174050",
      displayLabel: "Capítulo 8",
      pageCount: 12,
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/reader/123e4567-e89b-42d3-a456-426614174051", {
      headers: { Accept: "application/json" },
    });
  });

  it("loads a bounded page of verified reader chapters for one series", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174050";
    const chapterId = "123e4567-e89b-42d3-a456-426614174051";
    fetchMock.mockResolvedValue(mockResponse({
      data: [{
        chapter_id: chapterId,
        series_id: seriesId,
        display_label: "Capítulo 8",
        page_count: 12,
      }],
      meta: { limit: 100, offset: 200, total: 301 },
    }));

    await expect(getReaderChaptersPage({ seriesId, limit: 100, offset: 200 })).resolves.toEqual({
      data: [{ chapterId, seriesId, displayLabel: "Capítulo 8", pageCount: 12 }],
      meta: { limit: 100, offset: 200, total: 301 },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/reader/series/${seriesId}/chapters?limit=100&offset=200`,
      { headers: { Accept: "application/json" } },
    );
  });

  it("rejects reader chapter pages with mismatched or duplicate chapters", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174050";
    const chapter = {
      chapter_id: "123e4567-e89b-42d3-a456-426614174051",
      series_id: seriesId,
      display_label: "Capítulo 8",
      page_count: 12,
    };
    for (const data of [
      [{ ...chapter, series_id: "123e4567-e89b-42d3-a456-426614174052" }],
      [chapter, chapter],
    ]) {
      fetchMock.mockResolvedValueOnce(
        mockResponse({ data, meta: { limit: 100, offset: 0, total: data.length } }),
      );
      await expect(getReaderChaptersPage({ seriesId, limit: 100, offset: 0 })).rejects.toMatchObject({
        name: "ApiError",
        status: 200,
        retryable: false,
      });
    }
  });

  it("reads null progress and sends only a zero-based page_index when saving", async () => {
    const chapterId = "123e4567-e89b-42d3-a456-426614174051";
    fetchMock.mockResolvedValueOnce(mockResponse({ data: null, meta: {} }));
    await expect(getReaderProgress(chapterId)).resolves.toBeNull();
    expect(fetchMock).toHaveBeenLastCalledWith(`/api/v1/reader/${chapterId}/progress`, {
      headers: { Accept: "application/json" },
    });

    fetchMock.mockResolvedValueOnce(mockResponse({
      data: {
        series_id: "123e4567-e89b-42d3-a456-426614174050",
        chapter_id: chapterId,
        page_index: 7,
        percentage: "77.78",
        updated_at: "2026-09-27T15:00:00.123456Z",
      },
      meta: {},
    }));
    await expect(putReaderProgress(chapterId, 7)).resolves.toEqual({
      seriesId: "123e4567-e89b-42d3-a456-426614174050",
      chapterId,
      pageIndex: 7,
      percentage: "77.78",
      updatedAt: "2026-09-27T15:00:00.123456Z",
    });
    expect(fetchMock).toHaveBeenLastCalledWith(`/api/v1/reader/${chapterId}/progress`, {
      method: "PUT",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ page_index: 7 }),
    });
  });

  it("rejects malformed progress and invalid positions without making a request", async () => {
    const chapterId = "123e4567-e89b-42d3-a456-426614174051";
    await expect(putReaderProgress(chapterId, 1.5)).rejects.toBeInstanceOf(RangeError);
    await expect(putReaderProgress(chapterId, -1)).rejects.toBeInstanceOf(RangeError);
    await expect(putReaderProgress("not-a-uuid", 0)).rejects.toBeInstanceOf(RangeError);
    fetchMock.mockResolvedValue(mockResponse({
      data: {
        series_id: "123e4567-e89b-42d3-a456-426614174050",
        chapter_id: chapterId,
        page_index: 2,
        percentage: "100.01",
        updated_at: "2026-09-27T15:00:00Z",
      },
      meta: {},
    }));
    await expect(getReaderProgress(chapterId)).rejects.toMatchObject({ name: "ApiError" });
  });

  it("reads reader preferences and writes the complete typed settings object", async () => {
    const settings = {
      cache_limit_mb: 420,
      preload_ahead: 1,
      mode: "double" as const,
      rtl: true,
      e_reader: true,
    };
    fetchMock.mockResolvedValueOnce(mockResponse({
      data: settings,
      meta: {},
    }));
    await expect(getReaderSettings()).resolves.toEqual(settings);
    expect(fetchMock).toHaveBeenLastCalledWith("/api/v1/settings/reader", {
      headers: { Accept: "application/json" },
    });

    fetchMock.mockResolvedValueOnce(mockResponse({
      data: settings,
      meta: {},
    }));
    await expect(putReaderSettings(settings)).resolves.toEqual(settings);
    expect(fetchMock).toHaveBeenLastCalledWith("/api/v1/settings/reader", {
      method: "PUT",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
  });

  it("rejects invalid reader preferences and malformed settings responses", async () => {
    await expect(putReaderSettings({
      cache_limit_mb: 501,
      preload_ahead: 0,
      mode: "single",
      rtl: false,
      e_reader: false,
    })).rejects.toBeInstanceOf(RangeError);
    await expect(putReaderSettings({
      cache_limit_mb: 100,
      preload_ahead: 3,
      mode: "single",
      rtl: false,
      e_reader: false,
    })).rejects.toBeInstanceOf(RangeError);
    expect(fetchMock).not.toHaveBeenCalled();

    fetchMock.mockResolvedValueOnce(mockResponse({
      data: {
        cache_limit_mb: 500,
        preload_ahead: 3,
        mode: "single",
        rtl: false,
        e_reader: false,
      },
      meta: {},
    }));
    await expect(getReaderSettings()).rejects.toMatchObject({ name: "ApiError" });
  });

  it("registers an online chapter without creating a download", async () => {
    const chapterId = "123e4567-e89b-42d3-a456-426614174051";
    const seriesId = "123e4567-e89b-42d3-a456-426614174050";
    fetchMock.mockResolvedValue(mockResponse({ data: {
      chapter_id: chapterId, series_id: seriesId, display_label: "Capítulo 1",
    }, meta: { connector_id: "yomira.fmd2", remote_series_id: "series/ref" } }));
    await expect(registerOnlineReaderChapter("yomira.fmd2", "series/ref", "chapter/ref"))
      .resolves.toEqual({ chapterId, seriesId });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/v1/connectors/yomira.fmd2/series/series%2Fref/reader-chapter");
    expect(JSON.parse(fetchMock.mock.calls[0]![1]!.body as string)).toEqual({ chapter_remote_id: "chapter/ref" });
  });

  it("opens an empty memory session before loading the online chapter metadata", async () => {
    const chapterId = "123e4567-e89b-42d3-a456-426614174051";
    const seriesId = "123e4567-e89b-42d3-a456-426614174050";
    fetchMock.mockResolvedValueOnce(mockResponse({ data: {
      session_id: "123e4567-e89b-42d3-a456-426614174052", chapter_id: chapterId,
      state: "complete", page_count: 10_000, ready_pages: [], error_code: null,
    }, meta: {} })).mockResolvedValueOnce(mockResponse({ data: {
      chapter_id: chapterId, series_id: seriesId, display_label: "Capítulo 1", page_count: 10_000,
    }, meta: {} }));
    await expect(openOnlineReaderChapter(chapterId)).resolves.toMatchObject({ chapterId, pageCount: 10_000 });
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      `/api/v1/reader/${chapterId}/session`, `/api/v1/reader/${chapterId}`,
    ]);
  });

  it("rejects an unexpected online session before requesting images or metadata", async () => {
    fetchMock.mockResolvedValue(mockResponse({ data: { state: "complete", page_count: 0 }, meta: {} }));
    await expect(openOnlineReaderChapter("123e4567-e89b-42d3-a456-426614174051"))
      .rejects.toMatchObject({ name: "ApiError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("returns one bounded verified image blob for a 1-based page", async () => {
    fetchMock.mockResolvedValue(mockBinaryResponse());

    const result = await getReaderPageBlob(
      "123e4567-e89b-42d3-a456-426614174051",
      1,
    );

    expect(result.type).toBe("image/jpeg");
    expect(result.size).toBe(3);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/reader/123e4567-e89b-42d3-a456-426614174051/pages/1",
      { headers: { Accept: "image/jpeg, image/png, image/webp" }, cache: "no-store" },
    );
  });

  it("preserves a reader 404 as a traced API error", async () => {
    fetchMock.mockResolvedValue(mockBinaryResponse({ status: 404 }));

    await expect(
      getReaderPageBlob("123e4567-e89b-42d3-a456-426614174051", 1),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      code: "READ_ASSET_NOT_FOUND",
      traceId: "trace-reader-page",
    });
  });

  it("rejects invalid IDs and page numbers before making reader requests", async () => {
    await expect(getReaderChapter("not-a-uuid")).rejects.toBeInstanceOf(RangeError);
    await expect(
      getReaderPageBlob("123e4567-e89b-42d3-a456-426614174051", 0),
    ).rejects.toBeInstanceOf(RangeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("accepts only the supported API version", async () => {
    fetchMock.mockResolvedValue(mockResponse({ app_version: "0.1.0", api_version: 1 }));
    await expect(getSystemVersion()).resolves.toEqual({
      app_version: "0.1.0",
      api_version: 1,
    });

    fetchMock.mockResolvedValue(mockResponse({ app_version: "0.1.0", api_version: 2 }));
    await expect(getSystemVersion()).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      retryable: false,
    });
  });

  it("preserves the stable error code and trace ID from the API envelope", async () => {
    fetchMock.mockResolvedValue(
      mockResponse(
        {
          error: {
            code: "API_VALIDATION_FAILED",
            message: "Request does not match the published schema.",
            retryable: false,
            trace_id: "trace-123",
          },
        },
        422,
      ),
    );

    await expect(getSystemLiveness()).rejects.toMatchObject({
      code: "API_VALIDATION_FAILED",
      retryable: false,
      status: 422,
      traceId: "trace-123",
    });
  });

  it("does not trust malformed or uncatalogued server error codes", async () => {
    fetchMock.mockResolvedValue(
      mockResponse(
        {
          error: {
            code: "UNKNOWN_ERROR",
            message: "Untrusted message",
            retryable: true,
            trace_id: "trace-456",
          },
        },
        500,
      ),
    );

    await expect(getSystemLiveness()).rejects.toMatchObject({
      code: null,
      message: "La API rechazó la solicitud.",
      retryable: false,
      traceId: null,
    });
  });

  it("marks a transport failure as retryable without inventing a server code", async () => {
    fetchMock.mockRejectedValue(new TypeError("socket unavailable"));

    await expect(getSystemLiveness()).rejects.toMatchObject({
      code: null,
      retryable: true,
      status: null,
      traceId: null,
    });
  });

  it("requests and validates a paginated local library response", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [series("123e4567-e89b-42d3-a456-426614174000", "Berserk")],
        meta: { limit: 100, offset: 200, total: 301 },
      }),
    );

    await expect(getLibraryPage({ limit: 100, offset: 200 })).resolves.toEqual({
      data: [
        {
          seriesId: "123e4567-e89b-42d3-a456-426614174000",
          canonicalTitle: "berserk",
          displayTitle: "Berserk",
          author: null,
          genres: null,
          description: null,
          status: null,
          language: "ja",
          coverAssetId: null,
          createdAt: "2026-09-26T10:00:00Z",
          updatedAt: "2026-09-26T10:00:00Z",
        },
      ],
      meta: { limit: 100, offset: 200, total: 301 },
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/library?limit=100&offset=200", {
      headers: { Accept: "application/json" },
    });
  });

  it("requests bounded recent library entries without offset or totals", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [series("123e4567-e89b-42d3-a456-426614174000", "Berserk")],
        meta: { limit: 4 },
      }),
    );

    await expect(getRecentLibrary(4)).resolves.toEqual({
      data: [
        expect.objectContaining({
          seriesId: "123e4567-e89b-42d3-a456-426614174000",
          displayTitle: "Berserk",
        }),
      ],
      meta: { limit: 4 },
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/library/recent?limit=4", {
      headers: { Accept: "application/json" },
    });
  });

  it("rejects a recent library response whose limit does not match the request", async () => {
    fetchMock.mockResolvedValue(mockResponse({ data: [], meta: { limit: 5 } }));

    await expect(getRecentLibrary(4)).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      retryable: false,
    });
  });

  it("requests and validates recently saved reader progress", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [
          {
            series_id: "123e4567-e89b-42d3-a456-426614174000",
            display_title: "Berserk",
            chapter_id: "123e4567-e89b-42d3-a456-426614174001",
            display_label: "Capítulo 3",
            page_index: 2,
            percentage: "50.00",
            updated_at: "2026-09-27T10:00:00Z",
          },
        ],
        meta: { limit: 4 },
      }),
    );

    await expect(getRecentReaderProgress(4)).resolves.toEqual({
      data: [
        {
          seriesId: "123e4567-e89b-42d3-a456-426614174000",
          seriesDisplayTitle: "Berserk",
          chapterId: "123e4567-e89b-42d3-a456-426614174001",
          chapterDisplayLabel: "Capítulo 3",
          pageIndex: 2,
          percentage: "50.00",
          updatedAt: "2026-09-27T10:00:00Z",
        },
      ],
      meta: { limit: 4 },
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/reader/progress/recent?limit=4", {
      headers: { Accept: "application/json" },
    });
  });

  it("rejects malformed recent progress rows and duplicate chapter identities", async () => {
    const row = {
      series_id: "123e4567-e89b-42d3-a456-426614174000",
      display_title: "Berserk",
      chapter_id: "123e4567-e89b-42d3-a456-426614174001",
      display_label: "Capítulo 3",
      page_index: 2,
      percentage: "50.00",
      updated_at: "2026-09-27T10:00:00Z",
    };
    fetchMock.mockResolvedValueOnce(
      mockResponse({ data: [{ ...row, page_index: -1 }], meta: { limit: 4 } }),
    );
    await expect(getRecentReaderProgress(4)).rejects.toMatchObject({ name: "ApiError" });

    fetchMock.mockResolvedValueOnce(
      mockResponse({ data: [row, row], meta: { limit: 4 } }),
    );
    await expect(getRecentReaderProgress(4)).rejects.toMatchObject({ name: "ApiError" });
  });

  it("rejects out-of-range recent list limits before sending a request", async () => {
    await expect(getRecentLibrary(0)).rejects.toBeInstanceOf(RangeError);
    await expect(getRecentReaderProgress(21)).rejects.toBeInstanceOf(RangeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("includes a selected reading filter in the library request", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({ data: [], meta: { limit: 100, offset: 0, total: 0 } }),
    );

    await expect(
      getLibraryPage({ limit: 100, offset: 0, readingStatus: "reading" }),
    ).resolves.toEqual({ data: [], meta: { limit: 100, offset: 0, total: 0 } });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/library?limit=100&offset=0&reading_status=reading",
      { headers: { Accept: "application/json" } },
    );
  });

  it("rejects a malformed library payload instead of rendering partial records", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [series("not-a-uuid", "Berserk")],
        meta: { limit: 100, offset: 0, total: 1 },
      }),
    );

    await expect(getLibraryPage({ limit: 100, offset: 0 })).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      code: null,
      retryable: false,
    });
  });

  it("preserves a traced library service error for the page ErrorState", async () => {
    fetchMock.mockResolvedValue(
      mockResponse(
        {
          error: {
            code: "SYS_INTERNAL_ERROR",
            message: "Ocurrió un error interno del sistema.",
            retryable: false,
            trace_id: "trace-library-503",
          },
        },
        503,
      ),
    );

    await expect(getLibraryPage({ limit: 100, offset: 0 })).rejects.toMatchObject({
      code: "SYS_INTERNAL_ERROR",
      status: 503,
      traceId: "trace-library-503",
    });
  });

  it("requests and validates one local series detail", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    fetchMock.mockResolvedValue(
      mockResponse({ data: series(seriesId, "Berserk"), meta: {} }),
    );

    await expect(getSeriesDetail(seriesId)).resolves.toMatchObject({
      seriesId,
      canonicalTitle: "berserk",
      displayTitle: "Berserk",
      description: null,
      status: null,
      language: "ja",
      coverAssetId: null,
      createdAt: "2026-09-26T10:00:00Z",
      updatedAt: "2026-09-26T10:00:00Z",
      metadata: expect.arrayContaining([
        expect.objectContaining({
          field: "language",
          value: "ja",
          authority: "LOCAL_CANONICAL",
        }),
      ]),
    });
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/series/${seriesId}`, {
      headers: { Accept: "application/json" },
    });
  });

  it("keeps effective author and genres returned by the local library", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    fetchMock.mockResolvedValue(mockResponse({
      data: [{ ...series(seriesId, "Berserk"), author: "Kentaro Miura", genres: ["Fantasía", "Acción"] }],
      meta: { limit: 100, offset: 0, total: 1 },
    }));
    const page = await getLibraryPage({ limit: 100, offset: 0 });
    expect(page.data[0]).toMatchObject({ author: "Kentaro Miura", genres: ["Fantasía", "Acción"] });
  });

  it.each([
    { author: 42 },
    { author: [] },
    { genres: "Acción" },
    { genres: ["Acción", 42] },
    { genres: {} },
  ])("rejects invalid present author/genres types: %j", async (invalid) => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    fetchMock.mockResolvedValue(mockResponse({
      data: { ...series(seriesId, "Berserk"), ...invalid }, meta: {},
    }));
    await expect(getSeriesDetail(seriesId)).rejects.toMatchObject({
      name: "ApiError", status: 200, retryable: false,
    });
  });

  it("preserves explicitly empty author and genres instead of replacing overrides", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    fetchMock.mockResolvedValue(mockResponse({
      data: { ...series(seriesId, "Berserk"), author: "", genres: [] }, meta: {},
    }));
    await expect(getSeriesDetail(seriesId)).resolves.toMatchObject({ author: "", genres: [] });
  });

  it("builds only local versioned cover URLs and rejects invalid path identities", () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    const path = getSeriesCoverUrl(seriesId, "large", "asset:updated");
    const url = new URL(path, "http://127.0.0.1:8000");
    expect(url.pathname).toBe(`/api/v1/series/${seriesId}/cover`);
    expect(url.searchParams.get("variant")).toBe("large");
    expect(url.searchParams.get("v")).toBe("asset:updated");
    expect(() => getSeriesCoverUrl("https://example.test/cover.webp")).toThrow(TypeError);
    expect(() => getSeriesCoverUrl(seriesId, "invalid" as "small")).toThrow(TypeError);
  });

  it("refreshes metadata using POST and validates the complete detail response", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    fetchMock.mockResolvedValue(mockResponse({
      data: { ...series(seriesId, "Berserk"), author: "Kentaro Miura", genres: ["Fantasía"] },
      meta: {},
    }));
    await expect(refreshSeriesMetadata(seriesId)).resolves.toMatchObject({
      seriesId, author: "Kentaro Miura", genres: ["Fantasía"],
    });
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/series/${seriesId}/metadata/refresh`, {
      headers: { Accept: "application/json" }, method: "POST",
    });
    fetchMock.mockResolvedValue(mockResponse({ data: series("123e4567-e89b-42d3-a456-426614174001", "Other"), meta: {} }));
    await expect(refreshSeriesMetadata(seriesId)).rejects.toMatchObject({ name: "ApiError" });
  });

  it("fetches bounded external metadata candidates without remote image URLs", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    const remoteId = "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a";
    fetchMock.mockResolvedValue(mockResponse({
      data: [{ remote_id: remoteId, title: "Naruto", year: 1999, author: "Masashi Kishimoto" }],
      meta: { provider: "MangaDex", total: 61, limit: 10 },
    }));
    await expect(getExternalMetadataCandidates(seriesId)).resolves.toEqual({
      data: [{ remoteId, title: "Naruto", year: 1999, author: "Masashi Kishimoto" }],
      meta: { provider: "MangaDex", total: 61, limit: 10 },
    });
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/series/${seriesId}/metadata/external-candidates`, {
      headers: { Accept: "application/json" },
    });
  });

  it.each([
    { data: [{ remote_id: "not-a-uuid", title: "Naruto", year: 1999, author: null }] },
    { data: [{ remote_id: "6B1EB93E-473A-4AB3-9922-1A66D2A29A4A", title: "Naruto", year: null, author: null }] },
    { data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "", year: null, author: null }] },
    { data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "   ", year: null, author: null }] },
    { data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "Naruto", year: 0, author: null }] },
    { data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "Naruto", year: 10000, author: null }] },
    { data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "Naruto", year: 1999.5, author: null }] },
    { data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "Naruto", year: true, author: null }] },
    { data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "Naruto", year: null, author: [] }] },
    { data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "Naruto", year: null, author: null, cover_url: "https://example.test/cover.jpg" }] },
    { meta: { provider: "unknown", total: 1, limit: 10 } },
    { meta: { provider: "MangaDex", total: -1, limit: 10 } },
    { meta: { provider: "MangaDex", total: 0, limit: 10 } },
    { meta: { provider: "MangaDex", total: 1.5, limit: 10 } },
    { meta: { provider: "MangaDex", total: 1, limit: 100 } },
    { meta: { provider: "MangaDex", total: 1, limit: 10, query: "Naruto" } },
    { unexpected: true },
  ])("rejects malformed external candidate data: %j", async (invalid) => {
    fetchMock.mockResolvedValue(mockResponse({
      data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "Naruto", year: null, author: null }],
      meta: { provider: "MangaDex", total: 1, limit: 10 }, ...invalid,
    }));
    await expect(getExternalMetadataCandidates("123e4567-e89b-42d3-a456-426614174000"))
      .rejects.toMatchObject({ name: "ApiError", status: 200, retryable: false });
  });

  it("rejects duplicate and oversized external candidate lists", async () => {
    const candidate = { remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "Naruto", year: null, author: null };
    for (const count of [2, 11]) {
      fetchMock.mockResolvedValue(mockResponse({
        data: Array.from({ length: count }, () => candidate),
        meta: { provider: "MangaDex", total: count, limit: 10 },
      }));
      await expect(getExternalMetadataCandidates("123e4567-e89b-42d3-a456-426614174000"))
        .rejects.toMatchObject({ name: "ApiError" });
    }
  });

  it("applies an explicitly selected external record and checks the returned series identity", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    const remoteId = "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a";
    fetchMock.mockResolvedValue(mockResponse({ data: series(seriesId, "Naruto"), meta: {} }));
    await expect(applyExternalSeriesMetadata(seriesId, remoteId)).resolves.toMatchObject({ seriesId });
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/series/${seriesId}/metadata/external`, {
      method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ remote_id: remoteId }),
    });
    fetchMock.mockResolvedValue(mockResponse({
      data: series("123e4567-e89b-42d3-a456-426614174001", "Other"), meta: {},
    }));
    await expect(applyExternalSeriesMetadata(seriesId, remoteId)).rejects.toMatchObject({ name: "ApiError" });
  });

  it("rejects invalid local and remote identities before external metadata requests", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    await expect(getExternalMetadataCandidates("not-a-uuid")).rejects.toThrow(RangeError);
    await expect(applyExternalSeriesMetadata("not-a-uuid", seriesId)).rejects.toThrow(RangeError);
    await expect(applyExternalSeriesMetadata(seriesId, "https://example.test/manga")).rejects.toThrow(RangeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("preserves the refresh failure code and trace from the backend", async () => {
    fetchMock.mockResolvedValue(mockResponse({ error: {
      code: "SRC_NETWORK_ERROR", message: "La fuente no respondió.", retryable: true,
      trace_id: "trace-metadata-refresh",
    } }, 503));
    await expect(refreshSeriesMetadata("123e4567-e89b-42d3-a456-426614174000"))
      .rejects.toMatchObject({ code: "SRC_NETWORK_ERROR", traceId: "trace-metadata-refresh", status: 503 });
  });

  it("rejects malformed series detail payloads instead of displaying partial metadata", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({ data: series("not-a-uuid", "Berserk"), meta: {} }),
    );

    await expect(getSeriesDetail("123e4567-e89b-42d3-a456-426614174000")).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      code: null,
      retryable: false,
    });
  });

  it("rejects a detail response with an incomplete metadata provenance set", async () => {
    const record = series("123e4567-e89b-42d3-a456-426614174000", "Berserk");
    fetchMock.mockResolvedValue(
      mockResponse({ data: { ...record, metadata: record.metadata.slice(0, 2) }, meta: {} }),
    );

    await expect(getSeriesDetail("123e4567-e89b-42d3-a456-426614174000")).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      code: null,
      retryable: false,
    });
  });

  it("sends and clears explicit metadata overrides using the local API", async () => {
    fetchMock.mockResolvedValue(mockResponse(null, 204));

    await expect(
      setMetadataOverride("123e4567-e89b-42d3-a456-426614174000", "genres", ["Action"]),
    ).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/series/123e4567-e89b-42d3-a456-426614174000/metadata-overrides",
      {
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        method: "PUT",
        body: JSON.stringify({ field: "genres", value: ["Action"] }),
      },
    );

    await expect(
      clearMetadataOverride("123e4567-e89b-42d3-a456-426614174000", "genres"),
    ).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/series/123e4567-e89b-42d3-a456-426614174000/metadata-overrides/genres",
      { headers: { Accept: "application/json" }, method: "DELETE" },
    );
  });

  it("preserves the traced not-found error for a series detail", async () => {
    fetchMock.mockResolvedValue(
      mockResponse(
        {
          error: {
            code: "API_REQUEST_REJECTED",
            message: "La serie solicitada no existe.",
            retryable: false,
            trace_id: "trace-series-404",
          },
        },
        404,
      ),
    );

    await expect(getSeriesDetail("123e4567-e89b-42d3-a456-426614174000")).rejects.toMatchObject({
      code: "API_REQUEST_REJECTED",
      status: 404,
      traceId: "trace-series-404",
    });
  });
});
