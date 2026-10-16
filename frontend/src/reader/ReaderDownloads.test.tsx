import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { ReaderDownloads } from "./ReaderDownloads";

vi.mock("../api/client", () => ({ getReaderDownloadSource: vi.fn(),
  getConnectorChapters: vi.fn(), getChapterAvailability: vi.fn(), queueConnectorChapters: vi.fn() }));
const chapterId = "123e4567-e89b-42d3-a456-426614174051";
function Panel() {
  const [open, setOpen] = useState(false);
  return <ReaderDownloads chapterId={chapterId} open={open} onOpenChange={setOpen} />;
}
function renderPanel() {
  return render(<QueryClientProvider client={new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })}><MemoryRouter><Panel /></MemoryRouter></QueryClientProvider>);
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
});

describe("downloads inside the reader", () => {
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
    fireEvent.click(screen.getByRole("button", { name: "Descargar manga completo" }));
    await screen.findByText(/105 capítulos añadidos/);
    expect(vi.mocked(api.queueConnectorChapters).mock.calls[1]?.[2]).toHaveLength(105);
  });
  it("retains a selection across bounded chapter pages", async () => {
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Capítulo 2" }));
    fireEvent.click(screen.getByRole("button", { name: "Más capítulos" }));
    expect(screen.getAllByRole("checkbox")).toHaveLength(5);
    fireEvent.click(screen.getByRole("checkbox", { name: "Capítulo 105" }));
    fireEvent.click(screen.getByRole("button", { name: "Descargar selección (2)" }));
    await waitFor(() => expect(api.queueConnectorChapters).toHaveBeenCalledWith(
      "yomira.mock", "fixture", ["chapter-2", "chapter-105"]));
  });
  it("preserves partial queues on failure and lets the user retry", async () => {
    vi.mocked(api.queueConnectorChapters).mockRejectedValueOnce(new Error("source unavailable"));
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    const whole = await screen.findByRole("button", { name: "Descargar manga completo" });
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
});
