import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { SeriesDetailPage } from "../series/SeriesDetailPage";
import { LibraryPage } from "./LibraryPage";

const connection = vi.hoisted(() => ({ mobile: false }));
vi.mock("../settings/MobileAccessPanel", () => ({
  get isMobileConnection() { return connection.mobile; },
}));
vi.mock("../api/client", async (original) => ({ ...await original<typeof api>(),
  getSeriesDetail: vi.fn(), getLibraryPage: vi.fn(), getReaderChaptersPage: vi.fn(),
  getRecentReaderProgress: vi.fn(), getLocalImportStatus: vi.fn(),
}));
const seriesId = "123e4567-e89b-42d3-a456-426614174000";
const series: api.LibrarySeriesDetail = {
  seriesId, canonicalTitle: "Ficha de prueba", displayTitle: "Ficha de prueba", author: null,
  genres: null, description: null, status: null, language: null, coverAssetId: null,
  createdAt: "2026-10-31T00:00:00Z", updatedAt: "2026-10-31T00:00:00Z", metadata: [], readingSources: [],
};
beforeEach(() => {
  vi.mocked(api.getSeriesDetail).mockResolvedValue(series);
  vi.mocked(api.getLibraryPage).mockResolvedValue({ data: [series], meta: { limit: 100, offset: 0, total: 1 } });
  vi.mocked(api.getReaderChaptersPage).mockResolvedValue({ data: [{ seriesId,
    chapterId: "123e4567-e89b-42d3-a456-426614174001", displayLabel: "Capítulo 1", pageCount: 10,
  }], meta: { limit: 100, offset: 0, total: 1 } });
  vi.mocked(api.getRecentReaderProgress).mockResolvedValue({ data: [], meta: { limit: 20 } });
  vi.mocked(api.getLocalImportStatus).mockResolvedValue({ folder: "C:/Mangas", folders: [], scanning: false,
    importedChapters: 0, pendingMetadata: 0, metadataReview: 0, errors: [],
  });
});
function show(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/library" element={<LibraryPage />} />
    <Route path="/series/:seriesId" element={<SeriesDetailPage />} />
  </Routes></MemoryRouter></QueryClientProvider>);
}

it.each([true, false])("only offers PC folder controls on a local connection (mobile=%s)", async (mobile) => {
  connection.mobile = mobile;
  show("/library");
  await screen.findByRole("list", { name: "Series de la biblioteca" });
  if (mobile) expect(screen.queryByRole("heading", { name: "Carpetas de tus mangas" })).not.toBeInTheDocument();
  else expect(await screen.findByRole("button", { name: "Abrir carpeta principal" })).toBeInTheDocument();
});

it.each([true, false])("only offers USB export on a local connection (mobile=%s)", async (mobile) => {
  connection.mobile = mobile;
  show(`/series/${seriesId}`);
  await screen.findByRole("list", { name: "Capítulos disponibles para lectura" });
  if (mobile) {
    expect(screen.queryByText("Exportar manga a USB o lector conectado")).not.toBeInTheDocument();
    expect(screen.queryByText("Exportar y administrar archivos")).not.toBeInTheDocument();
  } else {
    expect(screen.getByText("Exportar manga a USB o lector conectado")).toBeInTheDocument();
    expect(screen.getByText("Exportar y administrar archivos")).toBeInTheDocument();
  }
});
