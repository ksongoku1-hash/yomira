import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { OnlineChapters } from "./OnlineChapters";

vi.mock("../api/client", async (original) => ({...await original<typeof api>(),
  getConnectorChapters: vi.fn(), registerOnlineReaderChapter: vi.fn(),
  getChapterAvailability: vi.fn(), queueConnectorChapters: vi.fn(),
}));
const seriesId = "123e4567-e89b-42d3-a456-426614174000";
const chapterId = "123e4567-e89b-42d3-a456-426614174051";
const sources: api.LibraryReadingSource[] = ["source-one", "source-two"].map((remoteSeriesId, index) => ({
  seriesSourceId: `123e4567-e89b-42d3-a456-42661417400${index + 1}`, connectorId: "yomira.mock",
  remoteSeriesId, displayName: `Fuente ${index + 1}`, author: "Autor", description: "Sinopsis",
  genres: ["Drama"], status: "completed", language: "es",
}));
const chapters = (count: number): api.ConnectorChaptersResponse => ({data: Array.from({length: count}, (_, i) => ({
  remoteId: `chapter-${i + 1}`, remoteUrl: null, remoteLabel: `${i + 1}`, chapterRawLabel: `${i + 1}`,
  chapterSortKey: [i + 1], language: "es", displayLabel: `Capítulo ${i + 1}`,
})), meta: {connectorId: "yomira.mock", remoteSeriesId: "source-one"}});
function Destination() {const location = useLocation(); return <p>{location.pathname}{location.search}</p>;}
function Harness() {
  const [sourceId, setSourceId] = useState(sources[0]!.seriesSourceId);
  const [downloadOpen, setDownloadOpen] = useState(false);
  return <><button type="button" onClick={() => setDownloadOpen(!downloadOpen)}>Descargar</button>
    <OnlineChapters seriesId={seriesId} sources={sources} source={sources.find((s) => s.seriesSourceId === sourceId)!} onSourceChange={setSourceId} downloadOpen={downloadOpen} /></>;
}
function open() {
  return render(<QueryClientProvider client={new QueryClient({defaultOptions: {queries: {retry: false, gcTime: 300_000}}})}>
    <MemoryRouter initialEntries={["/series"]}><Routes>
      <Route path="/series" element={<><a href="/library">Biblioteca</a><Harness /></>} />
      <Route path="/reader/:id" element={<Destination />} />
    </Routes></MemoryRouter>
  </QueryClientProvider>);
}
beforeEach(() => {
  vi.mocked(api.getChapterAvailability).mockResolvedValue({linked: true, readyRemoteIds: [], readyLabels: [], busyRemoteIds: []});
  vi.mocked(api.queueConnectorChapters).mockImplementation(async (_connector, _series, ids) => ids.length);
  vi.mocked(api.getConnectorChapters).mockResolvedValue(chapters(2));
  vi.mocked(api.registerOnlineReaderChapter).mockResolvedValue({chapterId, seriesId});
});
afterEach(() => vi.clearAllMocks());

describe("online chapters from a library series", () => {
  it("preserves selections across filters and sends only missing chapters through the existing queue", async () => {
    vi.mocked(api.getChapterAvailability).mockResolvedValue({linked: true, readyRemoteIds: ["chapter-1"], readyLabels: [], busyRemoteIds: ["chapter-2"]});
    vi.mocked(api.getConnectorChapters).mockResolvedValue(chapters(105));
    open();
    fireEvent.click(await screen.findByRole("button", {name: "Descargar"}));
    await waitFor(() => expect(screen.getByRole("checkbox", {name: "Seleccionar Capítulo 3"})).toBeEnabled());
    expect(screen.getByRole("checkbox", {name: "Seleccionar Capítulo 1"})).toBeDisabled();
    expect(screen.getByRole("checkbox", {name: "Seleccionar Capítulo 2"})).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", {name: "Seleccionar Capítulo 3"}));
    fireEvent.change(screen.getByRole("textbox", {name: "Buscar capítulo"}), {target: {value: "105"}});
    fireEvent.click(screen.getByRole("checkbox", {name: "Seleccionar Capítulo 105"}));
    fireEvent.click(screen.getByRole("button", {name: "Descargar selección (2)"}));
    await waitFor(() => expect(api.queueConnectorChapters).toHaveBeenCalledWith("yomira.mock", "source-one", ["chapter-3", "chapter-105"]));
    expect(api.registerOnlineReaderChapter).not.toHaveBeenCalled();
  });
  it("refreshes chapters without downloading and keeps downloads blocked when availability fails", async () => {
    vi.mocked(api.getChapterAvailability).mockRejectedValue(new Error("offline"));
    open();
    fireEvent.click(await screen.findByRole("button", {name: "Actualizar capítulos"}));
    await waitFor(() => expect(api.getConnectorChapters).toHaveBeenLastCalledWith("yomira.mock", "source-one", true));
    expect(api.queueConnectorChapters).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", {name: "Descargar"}));
    await screen.findByRole("button", {name: "Reintentar estados"});
    expect(screen.getByRole("checkbox", {name: "Seleccionar Capítulo 1"})).toBeDisabled();
  });
  it("opens a selected online chapter without requiring any local chapters", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", {name: "Leer online Capítulo 2"}));
    expect(await screen.findByText(`/reader/${chapterId}?online=1`)).toBeInTheDocument();
    expect(api.registerOnlineReaderChapter).toHaveBeenCalledWith("yomira.mock", "source-one", "chapter-2");
    expect(api.getConnectorChapters).toHaveBeenCalledTimes(1);
  });
  it("renders only 100 rows and can find or open any chapter of a large catalog", async () => {
    vi.mocked(api.getConnectorChapters).mockResolvedValue(chapters(10_000));
    open();
    const list = await screen.findByRole("list", {name: "Capítulos online"});
    expect(within(list).getAllByRole("listitem")).toHaveLength(100);
    fireEvent.change(screen.getByRole("textbox", {name: "Buscar capítulo"}), {target: {value: "10000"}});
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", {name: "Leer online Capítulo 10000"}));
    await waitFor(() => expect(api.registerOnlineReaderChapter).toHaveBeenCalledWith("yomira.mock", "source-one", "chapter-10000"));
  });
  it("reuses the list across source switches and explicitly refreshes on request", async () => {
    open();
    await screen.findByRole("button", {name: "Leer online Capítulo 1"});
    fireEvent.change(screen.getByRole("combobox", {name: "Fuente de lectura"}), {target: {value: sources[1]!.seriesSourceId}});
    await waitFor(() => expect(api.getConnectorChapters).toHaveBeenCalledTimes(2));
    fireEvent.change(screen.getByRole("combobox", {name: "Fuente de lectura"}), {target: {value: sources[0]!.seriesSourceId}});
    await screen.findByRole("button", {name: "Leer online Capítulo 1"});
    expect(api.getConnectorChapters).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", {name: "Actualizar capítulos"}));
    await waitFor(() => expect(api.getConnectorChapters).toHaveBeenLastCalledWith("yomira.mock", "source-one", true));
  });
  it("preserves the current series when registration fails or returns a different edition", async () => {
    vi.mocked(api.registerOnlineReaderChapter).mockRejectedValueOnce(new Error("Fuente sin conexión"))
      .mockResolvedValue({...{chapterId, seriesId, displayLabel: "Capítulo 1"}, seriesId: sources[0]!.seriesSourceId});
    open();
    fireEvent.click(await screen.findByRole("button", {name: "Leer online Capítulo 1"}));
    await screen.findByText("Fuente sin conexión");
    fireEvent.click(screen.getByRole("button", {name: "Leer online Capítulo 1"}));
    await screen.findByText(/La fuente cambió la edición/);
    expect(screen.getByRole("heading", {name: "Leer online"})).toBeInTheDocument();
  });
  it("does not navigate if registration finishes after leaving the series", async () => {
    let complete!: (value: Awaited<ReturnType<typeof api.registerOnlineReaderChapter>>) => void;
    vi.mocked(api.registerOnlineReaderChapter).mockImplementation(() => new Promise((resolve) => {complete = resolve;}));
    const view = open();
    fireEvent.click(await screen.findByRole("button", {name: "Leer online Capítulo 1"}));
    await waitFor(() => expect(api.registerOnlineReaderChapter).toHaveBeenCalled());
    view.unmount();
    await act(async () => complete({chapterId, seriesId}));
    expect(screen.queryByText(`/reader/${chapterId}?online=1`)).not.toBeInTheDocument();
  });
});
