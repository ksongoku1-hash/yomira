import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReaderChapter } from "../api/client";
import { SeriesExportAction } from "./SeriesExportAction";

const api = vi.hoisted(() => ({ getReaderChaptersPage: vi.fn(), createCbrExport: vi.fn() }));
vi.mock("../api/client", async (original) => ({
  ...await original<typeof import("../api/client")>(), ...api,
}));
const seriesId = "123e4567-e89b-42d3-a456-426614174000";
const chapters = Array.from({ length: 103 }, (_, index): ReaderChapter => ({
  chapterId: `123e4567-e89b-42d3-a456-${String(index + 1).padStart(12, "0")}`,
  seriesId, displayLabel: `Capítulo ${index + 1}`, pageCount: 10,
}));
function show() {
  render(<QueryClientProvider client={new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })}><SeriesExportAction seriesId={seriesId} /></QueryClientProvider>);
}
beforeEach(() => {
  vi.resetAllMocks();
  api.getReaderChaptersPage.mockImplementation(async ({ offset }) => ({
    data: chapters.slice(offset, offset + 100), meta: { limit: 100, offset, total: chapters.length },
  }));
  api.createCbrExport.mockRejectedValue(new Error("Detener después de comprobar la selección"));
});
describe("single manga file", () => {
  it("includes all chapter-list pages in one file and explains Books", async () => {
    show(); await screen.findByText("103 capítulos · 1030 páginas.");
    expect(screen.getByText(/En los lectores conectados se guarda en Books/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Cómo guardar el manga")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Crear CBR (103 capítulos)" }));
    await screen.findByText("No se pudo crear el CBR.");
    expect(api.createCbrExport).toHaveBeenCalledWith(seriesId, chapters.map(c => c.chapterId));
    expect(api.getReaderChaptersPage.mock.calls.map(([p]) => p.offset)).toEqual([0, 100]);
  });
  it("creates one file for an inclusive interval across chapter-list pages", async () => {
    show(); await screen.findByText("103 capítulos · 1030 páginas.");
    fireEvent.change(screen.getByLabelText("Qué capítulos exportar"), { target: { value: "range" } });
    fireEvent.change(screen.getByLabelText("Desde"), { target: { value: "99" } });
    fireEvent.change(screen.getByLabelText("Hasta"), { target: { value: "101" } });
    fireEvent.click(screen.getByRole("button", { name: "Crear CBR (3 capítulos)" }));
    await screen.findByText("No se pudo crear el CBR.");
    expect(api.createCbrExport).toHaveBeenCalledWith(seriesId, chapters.slice(99, 102).map(c => c.chapterId));
  });
  it.each(["changed total", "duplicate chapters", "missing page"])("refuses an inconsistent listing: %s", async problem => {
    api.getReaderChaptersPage.mockImplementation(async ({ offset }) => ({
      data: offset === 0 ? chapters.slice(0, 100) : problem === "missing page" ? []
        : problem === "duplicate chapters" ? chapters.slice(0, 3) : chapters.slice(100),
      meta: { limit: 100, offset, total: offset > 0 && problem === "changed total" ? 104 : 103 },
    }));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("La biblioteca cambió");
    expect(screen.getByRole("button", { name: "Crear CBR (0 capítulos)" })).toBeDisabled();
    expect(api.createCbrExport).not.toHaveBeenCalled();
  });
  it("does not send a partial manga when the page limit is exceeded", async () => {
    api.getReaderChaptersPage.mockResolvedValue({
      data: [{ ...chapters[0], pageCount: 10001 }], meta: { total: 1, limit: 100, offset: 0 },
    });
    show(); await screen.findByText(/Este grupo supera/);
    expect(screen.getByRole("button", { name: "Crear CBR (1 capítulo)" })).toBeDisabled();
    expect(api.createCbrExport).not.toHaveBeenCalled();
  });
});
