import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChapterExportAction } from "./ChapterExportAction";
import { UsbExportsPage } from "./UsbExportsPage";
import { SeriesDetailPage } from "../series/SeriesDetailPage";

function mockResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => payload,
  } as unknown as Response;
}

const target = {
  target_id: "E:00001234",
  display_name: "Bigme reader",
  file_system: "exFAT",
  total_bytes: 32_000_000_000,
  free_bytes: 28_000_000_000,
};

const exportJob = {
  export_id: "123e4567-e89b-42d3-a456-426614174010",
  asset_id: "123e4567-e89b-42d3-a456-426614174011",
  target_id: target.target_id,
  target_label: target.display_name,
  file_system: target.file_system,
  destination_name: "chapter-1.cbz",
  expected_size_bytes: 1234,
  expected_sha256: "a".repeat(64),
  status: "EXPORTED",
  created_at: "2026-09-28T12:00:00Z",
  updated_at: "2026-09-28T12:00:01Z",
  started_at: "2026-09-28T12:00:00Z",
  terminal_at: "2026-09-28T12:00:01Z",
  trace_id: "123e4567-e89b-42d3-a456-426614174012",
  error_code: null,
  failure_stage: null,
  retryable: null,
  cause_type: null,
};

function renderWithQueryClient(ui: React.ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderSeriesDetail() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/series/123e4567-e89b-42d3-a456-426614174040"]}>
        <Routes>
          <Route element={<SeriesDetailPage />} path="/series/:seriesId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("USB export UI", () => {
  it("marks an old failure recovered when a newer matching copy was verified", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input) => String(input).includes("/targets")
      ? mockResponse({ data: [] })
      : mockResponse({ data: [{ ...exportJob, status: "SKIPPED", created_at: "2026-09-28T12:00:01Z" },
        { ...exportJob, export_id: "123e4567-e89b-42d3-a456-426614174020", status: "FAILED" }],
        meta: { limit: 50, total: 2, next_cursor: null } })));
    renderWithQueryClient(<UsbExportsPage />);
    await screen.findByText("Resuelto por un envío posterior verificado.");
    expect(screen.queryByRole("button", { name: /Reintentar envío de/ })).not.toBeInTheDocument();
  });
  it("shows phase progress and disables retries during a transfer", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input) => String(input).includes("/targets")
      ? mockResponse({ data: [target] })
      : mockResponse({ data: [{ ...exportJob, status: "EXPORTING", terminal_at: null,
          progress: { stage: "verify_staging", processed_bytes: 617, total_bytes: 1234 } },
        { ...exportJob, export_id: "123e4567-e89b-42d3-a456-426614174020", status: "INTERRUPTED" }],
        meta: { limit: 50, total: 2, next_cursor: null } })));
    renderWithQueryClient(<UsbExportsPage />);
    expect(await screen.findByRole("progressbar")).toHaveAttribute("value", "617");
    expect(screen.getByText(/50% de esta etapa/)).toHaveTextContent("Comprobando la copia");
    expect(screen.getByRole("button", { name: /Reintentar envío de/ })).toBeDisabled();
  });

  it("retries a saved asset without rebuilding it and preserves the key after a lost response", async () => {
    const requests: RequestInit[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input, init) => {
      if (String(input).includes("/targets")) return mockResponse({ data: [target] });
      if (init?.method === "POST") {
        requests.push(init);
        if (requests.length === 1) throw new TypeError("connection lost");
        return mockResponse({ data: { ...exportJob, status: "SKIPPED" } });
      }
      return mockResponse({ data: [{ ...exportJob, status: "INTERRUPTED" }],
        meta: { limit: 50, total: 1, next_cursor: null } });
    }));
    renderWithQueryClient(<UsbExportsPage />);
    const button = await screen.findByRole("button", { name: /Reintentar envío de/ });
    fireEvent.click(button);
    await screen.findByRole("alert");
    fireEvent.click(button);
    await screen.findByText("Envío comprobado en el destino.");
    expect(requests).toHaveLength(2);
    expect(requests[0]!.headers).toEqual(requests[1]!.headers);
    expect(JSON.parse(String(requests[1]!.body))).toEqual({ asset_id: exportJob.asset_id, target_id: target.target_id });
  });
  it("shows durable export history and loads another keyset page", async () => {
    const secondJob = {
      ...exportJob,
      export_id: "123e4567-e89b-42d3-a456-426614174020",
      destination_name: "chapter-2.cbz",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(mockResponse({ data: [target] }))
      .mockResolvedValueOnce(
        mockResponse({ data: [exportJob], meta: { limit: 50, total: 2, next_cursor: "opaque-next" } }),
      )
      .mockResolvedValueOnce(
        mockResponse({ data: [secondJob], meta: { limit: 50, total: 2, next_cursor: null } }),
      );
    vi.stubGlobal("fetch", fetchMock);
    renderWithQueryClient(<UsbExportsPage />);

    expect(await screen.findByRole("heading", { name: "Exportaciones USB" })).toBeInTheDocument();
    expect(await screen.findByText("chapter-1.cbz")).toBeInTheDocument();
    expect(screen.getByText("Bigme reader · exFAT")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText("chapter-2.cbz")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/usb-exports/pages?limit=50&cursor=opaque-next",
      { headers: { Accept: "application/json" } },
    );
  });

  it("requires an explicit target choice before exporting a ready chapter", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(mockResponse({ data: [target] }))
      .mockResolvedValueOnce(mockResponse({ data: exportJob }));
    vi.stubGlobal("fetch", fetchMock);
    renderWithQueryClient(
      <ChapterExportAction chapterId="123e4567-e89b-42d3-a456-426614174030" chapterLabel="Capítulo 7" />,
    );

    const button = await screen.findByRole("button", { name: "Exportar Capítulo 7 a USB" });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Destino USB o lector para Capítulo 7"), {
      target: { value: target.target_id },
    });
    expect(button).toBeEnabled();
    fireEvent.click(button);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe("/api/v1/usb-exports/chapters/123e4567-e89b-42d3-a456-426614174030");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({
      Accept: "application/json",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(String(init.body))).toEqual({ target_id: target.target_id });
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toMatch(/^[a-f0-9-]{36}$/);
  });

  it("refreshes USB targets from a chapter after a device is connected", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(mockResponse({ data: [] }))
      .mockResolvedValueOnce(mockResponse({ data: [target] }))
      .mockResolvedValueOnce(mockResponse({ data: exportJob }));
    vi.stubGlobal("fetch", fetchMock);
    renderWithQueryClient(
      <ChapterExportAction chapterId="123e4567-e89b-42d3-a456-426614174030" chapterLabel="Capítulo 7" />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Buscar unidades otra vez" }));
    const selector = await screen.findByLabelText("Destino USB o lector para Capítulo 7");
    fireEvent.change(selector, { target: { value: target.target_id } });
    fireEvent.click(screen.getByRole("button", { name: "Exportar Capítulo 7 a USB" }));

    expect(await screen.findByText("CBZ exportado y verificado.")).toHaveAttribute("role", "status");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("offers whole manga USB export in the local series detail", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174040";
    const chapterId = "123e4567-e89b-42d3-a456-426614174041";
    const metadataFields = [
      ["title", "Mock Fixture Series", "LOCAL_CANONICAL"],
      ["cover", null, null],
      ["author", null, null],
      ["description", null, null],
      ["genres", null, null],
      ["status", null, null],
      ["language", "es", "LOCAL_CANONICAL"],
    ] as const;
    const metadata = metadataFields.map(([field, value, authority]) => ({
      field,
      value,
      authority,
      local_value: value,
      source_values: [],
      has_user_override: false,
      user_override_value: null,
      source_conflict: false,
      effective_source_ids: [],
    }));
    const seriesDetail = {
      series_id: seriesId,
      canonical_title: "mock fixture series",
      display_title: "Mock Fixture Series",
      description: null,
      status: null,
      language: "es",
      cover_asset_id: null,
      created_at: "2026-09-28T10:00:00Z",
      updated_at: "2026-09-28T10:00:00Z",
      metadata,
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/v1/series/")) {
        return Promise.resolve(mockResponse({ data: seriesDetail, meta: {} }));
      }
      if (url.startsWith("/api/v1/reader/series/")) {
        return Promise.resolve(
          mockResponse({
            data: [
              {
                chapter_id: chapterId,
                series_id: seriesId,
                display_label: "Capítulo 1",
                page_count: 1,
              },
            ],
            meta: { limit: 100, offset: 0, total: 1 },
          }),
        );
      }
      if (url === "/api/v1/cbr-exports") {
        return Promise.resolve(mockResponse({ data: { asset_id: exportJob.asset_id,
          filename: "manga.cbr", size_bytes: 1234, sha256: "a".repeat(64), page_count: 1,
          series_title: "Mock Fixture Series", chapter_labels: ["Capítulo 1"] } }));
      }
      if (url.startsWith("/api/v1/usb-exports/pages")) {
        return Promise.resolve(mockResponse({ data: [], meta: { limit: 50, total: 0, next_cursor: null } }));
      }
      if (url === "/api/v1/usb-exports/targets") {
        return Promise.resolve(mockResponse({ data: [target] }));
      }
      return Promise.resolve(mockResponse({ data: exportJob }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSeriesDetail();

    expect(await screen.findByRole("link", { name: "Leer Capítulo 1" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("Exportar manga a USB o lector conectado"));
    await screen.findByText("1 capítulo · 1 página.");
    fireEvent.click(screen.getByRole("button", { name: "Crear CBR (1 capítulo)" }));
    const select = await screen.findByLabelText("Destino USB o lector");
    fireEvent.change(select, { target: { value: target.target_id } });
    fireEvent.click(screen.getByRole("button", { name: "Exportar CBR a USB" }));

    expect(await screen.findByText("CBR exportado y verificado.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) =>
      String(input) === "/api/v1/usb-exports",
    )).toBe(true);
  });

  it("shows a durable failed export code and trace without claiming success", async () => {
    const failedJob = {
      ...exportJob,
      status: "FAILED",
      error_code: "EXP_DEVICE_DISCONNECTED",
      failure_stage: "copy",
      retryable: true,
      cause_type: "DeviceDisconnected",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(mockResponse({ data: [target] }))
      .mockResolvedValueOnce(mockResponse({ data: failedJob }));
    vi.stubGlobal("fetch", fetchMock);
    renderWithQueryClient(
      <ChapterExportAction chapterId="123e4567-e89b-42d3-a456-426614174030" chapterLabel="Capítulo 7" />,
    );
    fireEvent.change(await screen.findByLabelText("Destino USB o lector para Capítulo 7"), {
      target: { value: target.target_id },
    });
    fireEvent.click(screen.getByRole("button", { name: "Exportar Capítulo 7 a USB" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("no terminó");
    expect(alert).toHaveTextContent("EXP_DEVICE_DISCONNECTED");
    expect(alert).toHaveTextContent("123e4567-e89b-42d3-a456-426614174012");
    expect(screen.queryByText("CBZ exportado y verificado.")).not.toBeInTheDocument();
  });
});
