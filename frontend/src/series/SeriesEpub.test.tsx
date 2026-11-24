import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { LibrarySeriesDetail, ReaderChapter, SeriesEpubStatus } from "../api/client";
import { SeriesDetailPage } from "./SeriesDetailPage";
import { SeriesEpub } from "./SeriesEpub";

const api = vi.hoisted(() => ({ getSeriesEpubStatus: vi.fn(), createSeriesEpub: vi.fn(), getSeriesDetail: vi.fn(),
  getReaderChaptersPage: vi.fn(), getRecentReaderProgress: vi.fn() }));
vi.mock("../api/client", async original => ({ ...await original<typeof import("../api/client")>(), ...api }));
const seriesId = "123e4567-e89b-42d3-a456-426614174000";
const ready: SeriesEpubStatus = { status: "ready", filename: "Mi manga.epub", chapterCount: 103, pageCount: 1030,
  completedPages: 1030, sizeBytes: 12 * 1024 ** 2, hasFile: true, error: null };
const empty: SeriesEpubStatus = { status: "not_created", filename: null, chapterCount: 0, pageCount: 0,
  completedPages: 0, sizeBytes: null, hasFile: false, error: null };
const clients: QueryClient[] = [];

function client() {
  const value = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  clients.push(value); return value;
}
function show(count = 103) {
  const value = client();
  return render(<QueryClientProvider client={value}><SeriesEpub seriesId={seriesId} localChapterCount={count} /></QueryClientProvider>);
}
beforeEach(() => { vi.resetAllMocks(); api.getSeriesEpubStatus.mockResolvedValue(empty); api.createSeriesEpub.mockResolvedValue({ ...ready, status: "building", completedPages: 0 }); });
afterEach(() => { clients.forEach(value => value.clear()); clients.length = 0; vi.useRealTimers(); });

it("creates only on demand, with the series as its only argument", async () => {
  show(); const button = screen.getByRole("button", { name: "Crear EPUB" });
  await waitFor(() => expect(button).toBeEnabled());
  expect(api.createSeriesEpub).not.toHaveBeenCalled();
  expect(screen.getByText(/también los de otras páginas de la lista/)).toBeInTheDocument();
  fireEvent.click(button);
  await waitFor(() => expect(api.createSeriesEpub).toHaveBeenCalledExactlyOnceWith(seriesId));
  expect(await screen.findByRole("progressbar", { name: "Progreso del EPUB" })).toHaveAttribute("max", "1030");
  expect(screen.getByRole("button", { name: "Creando EPUB…" })).toBeDisabled();
});

it("shows the complete ready book, a manual update and a direct download without fetching the file", async () => {
  api.getSeriesEpubStatus.mockResolvedValue(ready); show();
  expect(await screen.findByText("EPUB listo: 103 capítulos · 1030 páginas.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Guardar EPUB" })).toHaveAttribute("href", `/api/v1/series/${seriesId}/epub/file`);
  expect(screen.getByRole("link", { name: "Guardar EPUB" })).toHaveAttribute("download");
  expect(screen.getByRole("button", { name: "Actualizar EPUB" })).toBeEnabled();
  expect(api.createSeriesEpub).not.toHaveBeenCalled();
});

it("explains new chapters without rebuilding automatically", async () => {
  api.getSeriesEpubStatus.mockResolvedValue({ ...ready, status: "outdated" }); show(104);
  expect(await screen.findByText(/Hay nuevos capítulos o cambios/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Actualizar EPUB" })).toBeEnabled();
  expect(api.createSeriesEpub).not.toHaveBeenCalled();
});

it("keeps the old download after a failed build and after a rejected update request", async () => {
  api.getSeriesEpubStatus.mockResolvedValue({ ...ready, status: "failed", error: "No hay espacio suficiente." });
  api.createSeriesEpub.mockRejectedValue(new Error("network failure")); show();
  expect(await screen.findByRole("alert")).toHaveTextContent("No hay espacio suficiente.");
  fireEvent.click(screen.getByRole("button", { name: "Actualizar EPUB" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("No se pudo iniciar el EPUB."));
  expect(screen.getByRole("link", { name: "Guardar EPUB" })).toHaveAttribute("href", `/api/v1/series/${seriesId}/epub/file`);
});

it("requires a local chapter but still permits saving an earlier book", async () => {
  api.getSeriesEpubStatus.mockResolvedValue(ready); show(0);
  await screen.findByRole("link", { name: "Guardar EPUB" });
  expect(screen.getByRole("button", { name: "Actualizar EPUB" })).toBeDisabled();
  expect(screen.getByText("Descarga al menos un capítulo para crear el libro.")).toBeInTheDocument();
});

it("polls progress only while building and stops when the file is ready", async () => {
  vi.useFakeTimers();
  api.getSeriesEpubStatus.mockResolvedValueOnce({ ...ready, status: "building", completedPages: 25 })
    .mockResolvedValueOnce({ ...ready, status: "building", completedPages: 500 }).mockResolvedValue(ready);
  show(); await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByRole("progressbar")).toHaveAttribute("value", "25");
  await act(async () => { await vi.advanceTimersByTimeAsync(2001); });
  expect(screen.getByRole("progressbar")).toHaveAttribute("value", "500");
  await act(async () => { await vi.advanceTimersByTimeAsync(2001); });
  expect(screen.getByText("EPUB listo: 103 capítulos · 1030 páginas.")).toBeInTheDocument();
  const requests = api.getSeriesEpubStatus.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(api.getSeriesEpubStatus).toHaveBeenCalledTimes(requests);
  expect(api.createSeriesEpub).not.toHaveBeenCalled();
});

it("shows indeterminate progress while the chapter pages are being prepared", async () => {
  api.getSeriesEpubStatus.mockResolvedValue({ ...empty, status: "building" }); show();
  await screen.findByText("Preparando capítulos del EPUB…");
  expect(screen.getByRole("progressbar", { name: "Progreso del EPUB" })).not.toHaveAttribute("value");
  expect(screen.getByRole("button", { name: "Creando EPUB…" })).toBeDisabled();
});

it("recovers a failed status request with an explicit check", async () => {
  api.getSeriesEpubStatus.mockRejectedValueOnce(new Error("invalid status")).mockResolvedValue(ready);
  show(); fireEvent.click(await screen.findByRole("button", { name: "Comprobar EPUB" }));
  expect(await screen.findByRole("link", { name: "Guardar EPUB" })).toBeInTheDocument();
});

it("integrates into the series detail and keeps the whole-manga request on another chapter-list page", async () => {
  const series: LibrarySeriesDetail = { seriesId, canonicalTitle: "Mi manga", displayTitle: "Mi manga", author: null,
    genres: [], description: null, status: null, language: "es", coverAssetId: null, createdAt: "2026-11-12T00:00:00Z",
    updatedAt: "2026-11-12T00:00:00Z", metadata: [], readingSources: [] };
  const chapters = Array.from({ length: 103 }, (_, index): ReaderChapter => ({ seriesId,
    chapterId: `123e4567-e89b-42d3-a456-${String(index + 1).padStart(12, "0")}`, displayLabel: `Capítulo ${index + 1}`, pageCount: 10 }));
  api.getSeriesDetail.mockResolvedValue(series); api.getRecentReaderProgress.mockResolvedValue({ data: [], meta: { limit: 20 } });
  api.getReaderChaptersPage.mockImplementation(async ({ offset = 0 }) => ({ data: chapters.slice(offset, offset + 100), meta: { limit: 100, offset, total: 103 } }));
  render(<QueryClientProvider client={client()}><MemoryRouter initialEntries={[`/series/${seriesId}`]}><Routes>
    <Route path="/series/:seriesId" element={<SeriesDetailPage />} /></Routes></MemoryRouter></QueryClientProvider>);
  await screen.findByRole("link", { name: "Leer Capítulo 1" });
  fireEvent.click(screen.getByRole("button", { name: "Siguiente" }));
  await screen.findByRole("link", { name: "Leer Capítulo 101" });
  const section = screen.getByRole("region", { name: "Tu libro EPUB" });
  const create = within(section).getByRole("button", { name: "Crear EPUB" });
  await waitFor(() => expect(create).toBeEnabled()); fireEvent.click(create);
  await waitFor(() => expect(api.createSeriesEpub).toHaveBeenCalledExactlyOnceWith(seriesId));
  expect(api.getReaderChaptersPage.mock.calls.some(([request]) => request.offset === 100)).toBe(true);
});
