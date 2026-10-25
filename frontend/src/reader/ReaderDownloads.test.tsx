import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { ReaderDownloads } from "./ReaderDownloads";

vi.mock("../api/client", () => ({ getReaderDownloadSource: vi.fn(),
  getConnectorChapters: vi.fn(), getChapterAvailability: vi.fn(), getReaderChaptersPage: vi.fn(), queueConnectorChapters: vi.fn() }));
const chapterId = "123e4567-e89b-42d3-a456-426614174051";
const seriesId = "123e4567-e89b-42d3-a456-426614174050";
const onRead = vi.fn();
function Panel({ browsing = false }: { browsing?: boolean }) {
  const [open, setOpen] = useState(false);
  return <ReaderDownloads chapterId={chapterId} open={open} onOpenChange={setOpen}
    {...(browsing ? { navigation: {chapter: { chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 }, onRead, isPending: false, isError: false } } : {})} />;
}
function renderPanel(browsing = false) {
  return render(<QueryClientProvider client={new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })}><MemoryRouter><Panel browsing={browsing} /></MemoryRouter></QueryClientProvider>);
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getReaderDownloadSource).mockResolvedValue({ connectorId: "yomira.mock",
    remoteSeriesId: "fixture", chapterRemoteId: "chapter-1" });
  vi.mocked(api.getConnectorChapters).mockResolvedValue({ data: Array.from({ length: 105 }, (_, index) => ({
    remoteId: `chapter-${index + 1}`, displayLabel: `Capítulo ${index + 1}`,
    remoteLabel: String(index + 1), chapterRawLabel: String(index + 1),
    chapterSortKey: [index + 1], language: null,
  })), meta: { connectorId: "yomira.mock", remoteSeriesId: "fixture" } });
  vi.mocked(api.getChapterAvailability).mockResolvedValue({ linked: true,
    readyRemoteIds: [], busyRemoteIds: [], readyLabels: [] });
  vi.mocked(api.queueConnectorChapters).mockImplementation(async (_connector, _series, ids) => ids.length);
  vi.mocked(api.getReaderChaptersPage).mockResolvedValue({ data: [
    {chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5},
    {chapterId: "223e4567-e89b-42d3-a456-426614174051", seriesId, displayLabel: "Capítulo 2", pageCount: 6},
  ], meta: {limit: 100, offset: 0, total: 2} });
});

describe("downloads inside the reader", () => {
  it("recognizes the current chapter and queues its fresh reference after an old link expires", async () => {
    vi.mocked(api.getReaderDownloadSource).mockResolvedValue({ connectorId: "yomira.mock",
      remoteSeriesId: "fixture", chapterRemoteId: "expired-link" });
    renderPanel(true);
    fireEvent.click(screen.getByRole("button", { name: "Lista de capítulos" }));
    expect(await screen.findByRole("button", { name: "Leer Capítulo 1" })).toBeDisabled();
    const download = screen.getByRole("button", { name: "Descargar capítulo actual" });
    await waitFor(() => expect(download).toBeEnabled());
    fireEvent.click(download);
    await waitFor(() => expect(api.queueConnectorChapters).toHaveBeenCalledExactlyOnceWith(
      "yomira.mock", "fixture", ["chapter-1"]));
  });

  it("keeps reading free of source lookups and queues only after explicit download", async () => {
    renderPanel();
    expect(api.getReaderDownloadSource).not.toHaveBeenCalled();
    expect(api.getConnectorChapters).not.toHaveBeenCalled();
    expect(api.queueConnectorChapters).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    const current = await screen.findByRole("button", { name: "Descargar capítulo actual" });
    await waitFor(() => expect(current).toBeEnabled());
    expect(screen.getAllByRole("checkbox")).toHaveLength(100);
    fireEvent.click(current);
    await waitFor(() => expect(api.queueConnectorChapters).toHaveBeenCalledWith("yomira.mock", "fixture", ["chapter-1"]));
    await screen.findByText(/1 capítulo añadido/);
    fireEvent.click(screen.getByText("Más opciones"));
    fireEvent.click(screen.getByRole("button", { name: "Descargar pendientes del manga (105)" }));
    await screen.findByText(/105 capítulos añadidos/);
    expect(vi.mocked(api.queueConnectorChapters).mock.calls[1]?.[2]).toHaveLength(105);
  });
  it("retains a selection across bounded chapter pages", async () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Capítulo 2" }));
    fireEvent.click(screen.getByRole("button", { name: "Más capítulos" }));
    expect(screen.getAllByRole("checkbox")).toHaveLength(5);
    expect(screen.getByText("1 seleccionado · 1 fuera de esta página")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Capítulo 105" }));
    fireEvent.click(screen.getByRole("button", { name: "Descargar selección (2)" }));
    await waitFor(() => expect(api.queueConnectorChapters).toHaveBeenCalledWith(
      "yomira.mock", "fixture", ["chapter-2", "chapter-105"]));
  });
  it("toggles only visible pending chapters and preserves selections on other pages", async () => {
    vi.mocked(api.getChapterAvailability).mockResolvedValue({linked: true,
      readyRemoteIds: ["chapter-1"], busyRemoteIds: ["chapter-2"], readyLabels: []});
    renderPanel();
    fireEvent.click(screen.getByRole("button", {name: "Descargar"}));
    const selectVisible = await screen.findByRole("button", {name: "Seleccionar pendientes visibles"});
    await waitFor(() => expect(selectVisible).toBeEnabled());
    fireEvent.click(screen.getByRole("checkbox", {name: "Capítulo 3"}));
    fireEvent.click(screen.getByRole("button", {name: "Más capítulos"}));
    fireEvent.click(screen.getByRole("button", {name: "Seleccionar pendientes visibles"}));
    expect(screen.getByText("6 seleccionados · 1 fuera de esta página")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Desmarcar visibles"}));
    expect(screen.getByText("1 seleccionado · 1 fuera de esta página")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", {name: "Capítulo 105"})).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", {name: "Capítulos anteriores"}));
    expect(screen.getByRole("checkbox", {name: "Capítulo 3"})).toBeChecked();
    fireEvent.click(screen.getByRole("button", {name: "Limpiar selección"}));
    expect(screen.getByText("0 seleccionados")).toBeInTheDocument();
    expect(api.queueConnectorChapters).not.toHaveBeenCalled();
  });
  it("clears only submitted selections after success and preserves all selections after failure", async () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", {name: "Descargar"}));
    const current = await screen.findByRole("button", {name: "Descargar capítulo actual"});
    await waitFor(() => expect(current).toBeEnabled());
    fireEvent.click(screen.getByRole("checkbox", {name: "Capítulo 2"}));
    fireEvent.click(screen.getByRole("checkbox", {name: "Capítulo 3"}));
    fireEvent.click(screen.getByRole("button", {name: "Descargar Capítulo 2"}));
    await screen.findByText(/1 capítulo añadido/);
    expect(screen.getByRole("checkbox", {name: "Capítulo 2"})).not.toBeChecked();
    expect(screen.getByRole("checkbox", {name: "Capítulo 3"})).toBeChecked();
    vi.mocked(api.queueConnectorChapters).mockRejectedValueOnce(new Error("offline"));
    const downloadSelected = screen.getByRole("button", {name: "Descargar selección (1)"});
    await waitFor(() => expect(downloadSelected).toBeEnabled());
    fireEvent.click(downloadSelected);
    await screen.findByRole("alert");
    expect(screen.getByRole("checkbox", {name: "Capítulo 3"})).toBeChecked();
    await waitFor(() => expect(downloadSelected).toBeEnabled());
    fireEvent.click(downloadSelected);
    await waitFor(() => expect(screen.getByRole("checkbox", {name: "Capítulo 3"})).not.toBeChecked());
  });
  it("preserves partial queues on failure and lets the user retry", async () => {
    vi.mocked(api.queueConnectorChapters).mockRejectedValueOnce(new Error("source unavailable"));
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    await screen.findByRole("button", { name: "Descargar capítulo actual" });
    fireEvent.click(screen.getByText("Más opciones"));
    const whole = await screen.findByRole("button", { name: "Descargar pendientes del manga (105)" });
    await waitFor(() => expect(whole).toBeEnabled());
    fireEvent.click(whole);
    expect(await screen.findByRole("alert")).toHaveTextContent("Los capítulos ya añadidos se conservan");
    fireEvent.click(whole);
    await screen.findByText(/105 capítulos añadidos/);
  });
  it("keeps local reading available when there is no authorized source", async () => {
    vi.mocked(api.getReaderDownloadSource).mockResolvedValue(null);
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    await screen.findByText(/Puedes seguir leyendo tu copia local/);
    expect(api.getConnectorChapters).not.toHaveBeenCalled();
    expect(api.queueConnectorChapters).not.toHaveBeenCalled();
  });
  it("downloads a single extra chapter without queuing the current one", async () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    const extra = await screen.findByRole("button", { name: "Descargar Capítulo 2" });
    await waitFor(() => expect(extra).toBeEnabled());
    fireEvent.click(extra);
    await waitFor(() => expect(api.queueConnectorChapters).toHaveBeenCalledExactlyOnceWith(
      "yomira.mock", "fixture", ["chapter-2"]));
  });
  it("disables individual downloads for saved and queued chapters", async () => {
    vi.mocked(api.getChapterAvailability).mockResolvedValue({ linked: true,
      readyRemoteIds: ["chapter-2"], busyRemoteIds: ["chapter-3"], readyLabels: [] });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    await screen.findByText("Guardado");
    expect(screen.getByRole("button", { name: "Descargar Capítulo 2" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Descargar Capítulo 3" })).toBeDisabled();
    expect(api.queueConnectorChapters).not.toHaveBeenCalled();
  });
  it("closes the panel, restores focus and keeps the selection on reopening", async () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Capítulo 2" }));
    fireEvent.click(screen.getByRole("button", { name: "Cerrar descargas" }));
    expect(screen.getByRole("button", { name: "Descargar" })).toHaveFocus();
    expect(screen.queryByRole("checkbox", { name: "Capítulo 2" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    expect(screen.getByRole("checkbox", { name: "Capítulo 2" })).toBeChecked();
    expect(api.queueConnectorChapters).not.toHaveBeenCalled();
  });
  it("searches and opens a chapter from the same source without downloading it", async () => {
    renderPanel(true);
    fireEvent.click(screen.getByRole("button", {name: "Lista de capítulos"}));
    fireEvent.change(await screen.findByLabelText("Buscar capítulo en el lector"), {target: {value: "105"}});
    fireEvent.click(await screen.findByRole("button", {name: "Leer Capítulo 105"}));
    expect(onRead).toHaveBeenCalledExactlyOnceWith({seriesId, connectorId: "yomira.mock", remoteSeriesId: "fixture", remoteId: "chapter-105", displayLabel: "Capítulo 105"});
    expect(api.queueConnectorChapters).not.toHaveBeenCalled();
  });
  it("offers saved chapters when no online source is available", async () => {
    vi.mocked(api.getReaderDownloadSource).mockResolvedValue(null);
    renderPanel(true);
    fireEvent.click(screen.getByRole("button", {name: "Lista de capítulos"}));
    const next = await screen.findByRole("button", {name: "Leer Capítulo 2"});
    expect(screen.getByRole("button", {name: "Leer Capítulo 1"})).toBeDisabled();
    fireEvent.click(next);
    expect(onRead).toHaveBeenCalledWith({chapterId: "223e4567-e89b-42d3-a456-426614174051", seriesId, displayLabel: "Capítulo 2", pageCount: 6});
    expect(api.getConnectorChapters).not.toHaveBeenCalled();
  });
  it("closes the chapter dropdown on an outside pointer without changing the chapter", async () => {
    renderPanel(true);
    fireEvent.click(screen.getByRole("button", {name: "Lista de capítulos"}));
    await screen.findByLabelText("Buscar capítulo en el lector");
    fireEvent.pointerDown(document.body);
    expect(screen.getByRole("button", {name: "Lista de capítulos"})).toHaveAttribute("aria-expanded", "false");
    expect(onRead).not.toHaveBeenCalled();
  });
});
