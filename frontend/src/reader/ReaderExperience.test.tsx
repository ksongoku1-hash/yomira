import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { ReaderPage } from "./ReaderPage";
import { saveVerticalAnchor } from "./readerPresentation";

vi.mock("../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api/client")>(),
  getReaderChapter: vi.fn(), getReaderChaptersPage: vi.fn(), getSeriesDetail: vi.fn(),
  getReaderPageBlob: vi.fn(), getReaderProgress: vi.fn(), getReaderSettings: vi.fn(),
  putReaderProgress: vi.fn(), putReaderSettings: vi.fn(),
  openOnlineReaderChapter: vi.fn(),
  getReaderDownloadSource: vi.fn(),
  getConnectorChapters: vi.fn(), registerOnlineReaderChapter: vi.fn(),
}));
const chapterId = "123e4567-e89b-42d3-a456-426614174051";
const seriesId = "123e4567-e89b-42d3-a456-426614174050";
const nextId = "223e4567-e89b-42d3-a456-426614174051";
const settings: api.ReaderSettings = { mode: "single", rtl: false, e_reader: false, preload_ahead: 2, cache_limit_mb: 16 };

function openReader(online = false, gcTime = 0, retry = false) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry, retryDelay: 0, gcTime } } })}>
    <MemoryRouter initialEntries={[`/reader/${chapterId}${online ? "?online=1" : ""}`]}><Routes><Route path="/reader/:chapterId" element={<ReaderPage />} /><Route path="/series/:seriesId" element={<h1>Ficha de la serie</h1>} /></Routes></MemoryRouter>
  </QueryClientProvider>);
}
function decode(image: HTMLElement, width: number, height: number) {
  Object.defineProperty(image, "naturalWidth", { configurable: true, value: width });
  Object.defineProperty(image, "naturalHeight", { configurable: true, value: height });
  fireEvent.load(image);
}
beforeEach(() => {
  vi.mocked(api.openOnlineReaderChapter).mockReset();
  vi.mocked(api.getReaderPageBlob).mockReset();
  vi.mocked(api.getReaderDownloadSource).mockResolvedValue(null);
  localStorage.clear();
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = vi.fn(() => "blob:experience");
    static revokeObjectURL = vi.fn();
  });
  vi.mocked(api.getReaderChapter).mockImplementation(async (id) => ({ chapterId: id, seriesId, displayLabel: id === chapterId ? "Capítulo 1" : "Capítulo 2", pageCount: 5 }));
  vi.mocked(api.getReaderChaptersPage).mockResolvedValue({ data: [
    { chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 },
    { chapterId: nextId, seriesId, displayLabel: "Capítulo 2", pageCount: 5 },
  ], meta: { limit: 100, offset: 0, total: 2 } });
  vi.mocked(api.getSeriesDetail).mockRejectedValue(new Error("unavailable metadata"));
  vi.mocked(api.getReaderSettings).mockResolvedValue(settings);
  vi.mocked(api.putReaderSettings).mockImplementation(async (value) => value);
  vi.mocked(api.getReaderProgress).mockResolvedValue(null);
  vi.mocked(api.putReaderProgress).mockImplementation(async (id, pageIndex) => ({ chapterId: id, seriesId, pageIndex, percentage: "20.00", updatedAt: "2026-10-10T00:00:00Z" }));
  vi.mocked(api.getReaderPageBlob).mockResolvedValue(new Blob(["page"], { type: "image/jpeg" }));
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); localStorage.clear(); });

describe("reading experience", () => {
  it("keeps the chosen zoom when navigating to the next chapter", async () => {
    openReader();
    await screen.findByRole("img", {name: "Capítulo 1, página 1 de 5"});
    fireEvent.click(screen.getByRole("button", {name: "Opciones de lectura"}));
    fireEvent.click(screen.getByRole("button", {name: "Acercar"}));
    fireEvent.click(screen.getByRole("button", {name: "Cerrar configuración"}));
    fireEvent.change(screen.getByRole("slider", {name: "Progreso del capítulo"}), {target: {value: "5"}});
    fireEvent.click(await screen.findByRole("link", {name: "Siguiente capítulo · Capítulo 2"}));
    await screen.findByRole("img", {name: "Capítulo 2, página 1 de 5"});
    fireEvent.click(screen.getByRole("button", {name: "Opciones de lectura"}));
    expect(screen.getByText("125 %")).toBeInTheDocument();
  });
  it("keeps manual controls hidden during mouse movement, restores their preference and ignores the shortcut in input fields", async () => {
    const view = openReader();
    await screen.findByRole("img", {name: "Capítulo 1, página 1 de 5"});
    fireEvent.click(screen.getByRole("button", {name: "Opciones de lectura"}));
    const visibility = screen.getByRole("combobox", {name: "Visibilidad de controles"});
    fireEvent.change(visibility, {target: {value: "manual"}});
    fireEvent.keyDown(visibility, {key: "h"});
    expect(screen.getByRole("button", {name: "Cerrar configuración"})).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Cerrar configuración"}));
    const reader = screen.getByRole("region", {name: "Superficie de lectura"});
    fireEvent.keyDown(reader, {key: "h"});
    expect(screen.queryByRole("button", {name: "Opciones de lectura"})).not.toBeInTheDocument();
    fireEvent.mouseMove(reader, {clientY: 20});
    expect(screen.queryByRole("button", {name: "Opciones de lectura"})).not.toBeInTheDocument();
    fireEvent.keyDown(reader, {key: "h"});
    expect(screen.getByRole("button", {name: "Opciones de lectura"})).toBeInTheDocument();
    view.unmount();
    openReader();
    fireEvent.click(await screen.findByRole("button", {name: "Mostrar controles"}));
    fireEvent.click(await screen.findByRole("button", {name: "Opciones de lectura"}));
    expect(screen.getByRole("combobox", {name: "Visibilidad de controles"})).toHaveValue("manual");
  });
  it("reveals automatic controls only near the edges during vertical reading and cleans up the timer", async () => {
    vi.mocked(api.getReaderSettings).mockResolvedValue({...settings, mode: "vertical"});
    const view = openReader();
    await screen.findByRole("img", {name: "Capítulo 1, página 1 de 5"});
    const viewport = screen.getByRole("region", {name: "Páginas en lectura vertical"});
    const root = viewport.closest("section")!;
    vi.spyOn(root, "getBoundingClientRect").mockReturnValue({top: 0, bottom: 800} as DOMRect);
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", {name: "Modo inmersivo"}));
    act(() => vi.advanceTimersByTime(3100));
    fireEvent.mouseMove(viewport, {clientY: 400});
    fireEvent.scroll(viewport);
    expect(screen.queryByRole("button", {name: "Opciones de lectura"})).not.toBeInTheDocument();
    fireEvent.mouseMove(viewport, {clientY: 20});
    expect(screen.getByRole("button", {name: "Opciones de lectura"})).toBeInTheDocument();
    view.unmount();
    expect(() => act(() => vi.advanceTimersByTime(4000))).not.toThrow();
  });
  it("navigates with the progress slider and can remove it from the toolbar", async () => {
    openReader();
    await screen.findByRole("img", {name: "Capítulo 1, página 1 de 5"});
    fireEvent.change(screen.getByRole("slider", {name: "Progreso del capítulo"}), {target: {value: "3"}});
    expect(screen.getByRole("button", {name: "Página 3 de 5"})).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Opciones de lectura"}));
    fireEvent.click(screen.getByRole("checkbox", {name: "Barra de progreso"}));
    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
  });
  it("keeps downloads, options and page navigation from covering each other and closes them with Escape", async () => {
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    expect(screen.getByRole("button", { name: "Acercar" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    await screen.findByText(/Puedes seguir leyendo tu copia local/);
    expect(screen.queryByRole("button", { name: "Acercar" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Página 1 de 5" }));
    expect(screen.getByRole("spinbutton", { name: "Ir a la página" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Descargar" })).toHaveAttribute("aria-expanded", "false");
    fireEvent.keyDown(screen.getByRole("spinbutton"), { key: "Escape" });
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  });
  it.each(["single", "vertical"] as const)("scrolls inside tall %s pages with the keyboard instead of skipping their contents", async (mode) => {
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode });
    openReader();
    decode(await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" }), 400, 4000);
    const viewport = screen.getByRole("region", { name: mode === "vertical" ? "Páginas en lectura vertical" : "Superficie de lectura" });
    Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 600 });
    Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 4000 });
    for (const key of ["PageDown", "PageUp", "ArrowDown", "ArrowUp"]) {
      const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      fireEvent(viewport, event);
      expect(event.defaultPrevented).toBe(false);
      expect(screen.getByRole("button", { name: "Página 1 de 5" })).toBeInTheDocument();
    }
  });
  it("reopens a lost online session on retry, releases its cache and saves the current page only after decoding it", async () => {
    let generation = 0;
    let complete!: (chapter: api.ReaderChapter) => void;
    vi.mocked(api.openOnlineReaderChapter).mockImplementation(async () => {
      generation++;
      if (generation === 2) return new Promise((resolve) => { complete = resolve; });
      return { chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 };
    });
    vi.mocked(api.getReaderProgress).mockResolvedValue({ chapterId, seriesId, pageIndex: 0,
      percentage: "20.00", updatedAt: "2026-10-10T00:00:00Z" });
    vi.mocked(api.getReaderPageBlob).mockImplementation(async (_id, pageNumber) => {
      if (generation === 1 && pageNumber === 2) throw new Error("Source session lost");
      return new Blob(["page"], { type: "image/jpeg" });
    });
    openReader(true);
    decode(await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" }), 400, 600);
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    fireEvent.click(await screen.findByRole("button", { name: "Reintentar página" }));
    await screen.findByText("Cargando información del capítulo…");
    expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(2);
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(2);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:experience");
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    await act(async () => { complete({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 }); });
    const restored = await screen.findByRole("img", { name: "Capítulo 1, página 2 de 5" });
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    decode(restored, 400, 600);
    await waitFor(() => expect(api.putReaderProgress).toHaveBeenCalledWith(chapterId, 1));
    fireEvent.click(screen.getByRole("button", { name: "Página anterior" }));
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    expect(vi.mocked(api.getReaderPageBlob).mock.calls.filter(([, pageNumber]) => pageNumber === 1)).toHaveLength(2);
  });
  it("keeps recovery bounded when reopening fails, and retries again only at the user's request", async () => {
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValueOnce({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 })
      .mockRejectedValueOnce(new Error("Source unavailable"))
      .mockResolvedValue({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 });
    vi.mocked(api.getReaderProgress).mockResolvedValue({ chapterId, seriesId, pageIndex: 2,
      percentage: "60.00", updatedAt: "2026-10-10T00:00:00Z" });
    vi.mocked(api.getReaderPageBlob).mockImplementation(async (_id, pageNumber) => {
      if (pageNumber === 3 && vi.mocked(api.openOnlineReaderChapter).mock.calls.length < 3) throw new Error("Source session lost");
      return new Blob(["page"], { type: "image/jpeg" });
    });
    openReader(true, 0, true);
    await screen.findByRole("button", { name: "Página 3 de 5" });
    fireEvent.click(await screen.findByRole("button", { name: "Reintentar página" }));
    await screen.findByRole("heading", { name: "No se pudo abrir el capítulo" });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
    expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(2);
    const requestsBeforeRetry = vi.mocked(api.getReaderPageBlob).mock.calls.length;
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    await screen.findByRole("img", { name: "Capítulo 1, página 3 de 5" });
    expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(3);
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(requestsBeforeRetry + 1);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });
  it.each([
    { pageCount: 1, seriesId }, { pageCount: 7, seriesId },
    { pageCount: 5, seriesId: "323e4567-e89b-42d3-a456-426614174050" },
  ])("stops recovery when the source changes the chapter: %j", async (changed) => {
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValueOnce({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 })
      .mockResolvedValue({ chapterId, displayLabel: "Capítulo 1", ...changed });
    vi.mocked(api.getReaderProgress).mockResolvedValue({ chapterId, seriesId, pageIndex: 2,
      percentage: "60.00", updatedAt: "2026-10-10T00:00:00Z" });
    vi.mocked(api.getReaderPageBlob).mockRejectedValue(new Error("Source session lost"));
    openReader(true);
    await screen.findByRole("button", { name: "Página 3 de 5" });
    const requests = vi.mocked(api.getReaderPageBlob).mock.calls.length;
    fireEvent.click(await screen.findByRole("button", { name: "Reintentar página" }));
    await screen.findByText(/La fuente cambió el capítulo/);
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(requests);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    await waitFor(() => expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(3));
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(requests);
    expect(screen.getByText(/tu progreso guardado se conserva/)).toBeInTheDocument();
  });
  it("retries a local page without opening an online session", async () => {
    vi.mocked(api.getReaderPageBlob).mockRejectedValueOnce(new Error("Page unavailable"));
    openReader();
    fireEvent.click(await screen.findByRole("button", { name: "Reintentar página" }));
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(2);
    expect(api.openOnlineReaderChapter).not.toHaveBeenCalled();
  });
  it("does not return to the reader if recovery finishes after leaving the chapter", async () => {
    let complete!: (chapter: api.ReaderChapter) => void;
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValueOnce({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 })
      .mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
    vi.mocked(api.getReaderPageBlob).mockRejectedValueOnce(new Error("Source session lost"));
    openReader(true);
    fireEvent.click(await screen.findByRole("button", { name: "Reintentar página" }));
    await screen.findByText("Cargando información del capítulo…");
    fireEvent.click(screen.getByRole("link", { name: "← Volver a la serie" }));
    await act(async () => { complete({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 }); });
    expect(screen.getByRole("heading", { name: "Ficha de la serie" })).toBeInTheDocument();
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(1);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });
  function onlineCatalog() {
    vi.mocked(api.openOnlineReaderChapter).mockImplementation(async (id) => ({ chapterId: id, seriesId,
      displayLabel: id === chapterId ? "Capítulo 1" : "Capítulo 2", pageCount: 2 }));
    vi.mocked(api.getReaderDownloadSource).mockImplementation(async (id) => ({ connectorId: "yomira.mock",
      remoteSeriesId: "series", chapterRemoteId: id === chapterId ? "remote-1" : "remote-2" }));
    const item = (remoteId: string | null, chapter: number, language = "es"): api.ConnectorChapter => ({
      remoteId, displayLabel: `Capítulo ${chapter}`, remoteLabel: null,
      chapterRawLabel: String(chapter), chapterSortKey: String(chapter).split(".").map(Number), language,
    });
    vi.mocked(api.getConnectorChapters).mockResolvedValue({ data: [item("remote-2", 2), item("en", 1.5, "en"),
      item(null, 1.7), item("remote-1", 1)], meta: { connectorId: "yomira.mock", remoteSeriesId: "series" } });
    vi.mocked(api.registerOnlineReaderChapter).mockImplementation(async (_connector, _series, remote) => ({
      chapterId: remote === "remote-1" ? chapterId : nextId, seriesId,
    }));
  }
  it("moves online through the source catalog and returns with canonical progress, without consulting local-only chapters", async () => {
    onlineCatalog();
    let active = "";
    vi.mocked(api.openOnlineReaderChapter).mockImplementation(async (id) => {
      await Promise.resolve();
      active = id;
      return { chapterId: id, seriesId, displayLabel: id === chapterId ? "Capítulo 1" : "Capítulo 2", pageCount: 2 };
    });
    vi.mocked(api.getReaderPageBlob).mockImplementation(async (id) => {
      if (id !== active) throw new Error("Chapter session is not open");
      return new Blob(["page"], { type: "image/jpeg" });
    });
    vi.mocked(api.getReaderProgress).mockImplementation(async (id) => {
      if (id !== active) throw new Error("Chapter session is not open");
      return { chapterId: id, seriesId, pageIndex: id === chapterId ? 1 : 0,
        percentage: "50.00", updatedAt: "2026-10-10T00:00:00Z" };
    });
    openReader(true, 60_000);
    await screen.findByRole("img", { name: "Capítulo 1, página 2 de 2" });
    expect(api.registerOnlineReaderChapter).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Siguiente capítulo · Capítulo 2" }));
    const second = await screen.findByRole("img", { name: "Capítulo 2, página 1 de 2" });
    expect(api.openOnlineReaderChapter).toHaveBeenCalledWith(nextId);
    expect(api.registerOnlineReaderChapter).toHaveBeenCalledWith("yomira.mock", "series", "remote-2");
    expect(api.getReaderChaptersPage).not.toHaveBeenCalled();
    expect(api.getReaderChapter).not.toHaveBeenCalled();
    decode(second, 400, 600);
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    await screen.findByRole("img", { name: "Capítulo 2, página 2 de 2" });
    fireEvent.click(await screen.findByRole("button", { name: "Capítulo anterior" }));
    await screen.findByRole("img", { name: "Capítulo 1, página 2 de 2" });
    expect(api.getConnectorChapters).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.openOnlineReaderChapter).mock.calls.filter(([id]) => id === chapterId)).toHaveLength(2);
    expect(api.registerOnlineReaderChapter).toHaveBeenLastCalledWith("yomira.mock", "series", "remote-1");
  });
  it("keeps the current online chapter when registration fails and lets the reader retry", async () => {
    onlineCatalog();
    vi.mocked(api.registerOnlineReaderChapter).mockRejectedValueOnce(new Error("Source unavailable"));
    openReader(true);
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 2" });
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    fireEvent.click(await screen.findByRole("button", { name: "Siguiente capítulo · Capítulo 2" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("heading", { name: "Capítulo 1" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Siguiente capítulo · Capítulo 2" }));
    await screen.findByRole("heading", { name: "Capítulo 2" });
  });
  it("rejects a chapter registration that changes the canonical series", async () => {
    onlineCatalog();
    vi.mocked(api.registerOnlineReaderChapter).mockResolvedValue({ chapterId: nextId,
      seriesId: "323e4567-e89b-42d3-a456-426614174050" });
    openReader(true);
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 2" });
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    fireEvent.click(await screen.findByRole("button", { name: "Siguiente capítulo · Capítulo 2" }));
    await screen.findByRole("alert");
    expect(api.openOnlineReaderChapter).not.toHaveBeenCalledWith(nextId);
  });
  it("does not navigate back into the reader when a registration finishes after leaving it", async () => {
    onlineCatalog();
    let complete!: (value: { chapterId: string; seriesId: string }) => void;
    vi.mocked(api.registerOnlineReaderChapter).mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
    openReader(true);
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 2" });
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    fireEvent.click(await screen.findByRole("button", { name: "Siguiente capítulo · Capítulo 2" }));
    await screen.findByText("Abriendo capítulo online…");
    fireEvent.click(screen.getByRole("link", { name: "← Volver a la serie" }));
    await act(async () => { complete({ chapterId: nextId, seriesId }); });
    expect(screen.getByRole("heading", { name: "Ficha de la serie" })).toBeInTheDocument();
    expect(api.openOnlineReaderChapter).not.toHaveBeenCalledWith(nextId);
  });
  it("uses the online target when continuing from the last page with automatic continuation enabled", async () => {
    onlineCatalog();
    openReader(true);
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 2" });
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Continuar al siguiente capítulo automáticamente" }));
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    await screen.findByRole("button", { name: "Siguiente capítulo · Capítulo 2" });
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    await screen.findByRole("img", { name: "Capítulo 2, página 1 de 2" });
    expect(api.openOnlineReaderChapter).toHaveBeenCalledWith(nextId);
  });
  it("keeps the download panel open while immersive controls are inactive", async () => {
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    fireEvent.click(screen.getByRole("button", { name: "Modo inmersivo" }));
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    await screen.findByText(/Puedes seguir leyendo tu copia local/);
    vi.useFakeTimers();
    act(() => { vi.advanceTimersByTime(3100); });
    expect(screen.getByRole("heading", { name: "Guardar para leer sin conexión" })).toBeInTheDocument();
  });
  it("opens online through the existing reader and waits for a displayed page before saving it", async () => {
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 });
    openReader(true);
    const first = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    expect(api.openOnlineReaderChapter).toHaveBeenCalledWith(chapterId);
    expect(api.getReaderChapter).not.toHaveBeenCalled();
    expect(screen.getByText("Online · descarga opcional")).toBeInTheDocument();
    decode(first, 400, 600);
    await waitFor(() => expect(api.putReaderProgress).toHaveBeenCalledWith(chapterId, 0));
    vi.mocked(api.putReaderProgress).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    const second = await screen.findByRole("img", { name: "Capítulo 1, página 2 de 5" });
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    decode(second, 400, 600);
    await waitFor(() => expect(api.putReaderProgress).toHaveBeenCalledWith(chapterId, 1));
  });
  it("does not overwrite a restored final page during automatic viewport scrolling", async () => {
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode: "vertical" });
    vi.mocked(api.getReaderProgress).mockResolvedValue({ chapterId, seriesId, pageIndex: 4, percentage: "100.00", updatedAt: "2026-10-10T00:00:00Z" });
    openReader();
    const image = await screen.findByRole("img", { name: "Capítulo 1, página 5 de 5" });
    decode(image, 400, 400);
    const viewport = screen.getByRole("region", { name: "Páginas en lectura vertical" });
    fireEvent.scroll(viewport, { target: { scrollTop: 2000 } });
    expect(screen.getByRole("button", { name: "Página 5 de 5" })).toBeInTheDocument();
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });

  it("pinches to zoom and never turns a page while panning a zoomed image", async () => {
    class TestPointerEvent extends MouseEvent {
      pointerId: number;
      pointerType: string;
      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init); this.pointerId = init.pointerId ?? 0; this.pointerType = init.pointerType ?? "touch";
      }
    }
    vi.stubGlobal("PointerEvent", TestPointerEvent);
    openReader();
    const image = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    const canvas = image.parentElement!;
    canvas.style.padding = "0px";
    Object.defineProperty(canvas, "clientWidth", { configurable: true, value: 1000 });
    Object.defineProperty(canvas, "clientHeight", { configurable: true, value: 600 });
    fireEvent(window, new Event("resize"));
    decode(image, 400, 600);
    fireEvent.pointerDown(canvas, { pointerId: 1, pointerType: "touch", clientX: 100, clientY: 100 });
    fireEvent.pointerDown(canvas, { pointerId: 2, pointerType: "touch", clientX: 200, clientY: 100 });
    fireEvent.pointerMove(canvas, { pointerId: 2, pointerType: "touch", clientX: 300, clientY: 100 });
    expect(image.style.width).toBe("800px");
    expect(image.style.transform).toBe("");
    fireEvent.pointerUp(canvas, { pointerId: 2, pointerType: "touch", clientX: 300, clientY: 100 });
    fireEvent.pointerUp(canvas, { pointerId: 1, pointerType: "touch", clientX: 100, clientY: 100 });
    expect(screen.getByRole("button", { name: "Página 1 de 5" })).toBeInTheDocument();
  });

  it("measures the canvas after chapter loading and refits when its available height changes", async () => {
    let resize!: () => void;
    const observed = new Set<Element>();
    const disconnect = vi.fn();
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: () => void) { resize = callback; }
      observe(element: Element) { observed.add(element); }
      disconnect = disconnect;
    });
    const view = openReader();
    const image = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    const canvas = screen.getByRole("region", { name: "Superficie de lectura" });
    expect(observed.has(canvas)).toBe(true);
    expect(observed.has(screen.getByRole("heading", {name: "Capítulo 1"}).closest("header")!)).toBe(true);
    canvas.style.padding = "0px";
    Object.defineProperty(canvas, "clientWidth", { configurable: true, value: 1000 });
    Object.defineProperty(canvas, "clientHeight", { configurable: true, value: 600 });
    act(resize);
    decode(image, 400, 600);
    expect(image.style.width).toBe("400px");
    Object.defineProperty(canvas, "clientHeight", { configurable: true, value: 300 });
    act(resize);
    expect(image.style.width).toBe("200px");
    canvas.scrollTop = 120;
    canvas.scrollLeft = 90;
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    await screen.findByRole("img", { name: "Capítulo 1, página 2 de 5" });
    expect(canvas.scrollTop).toBe(0);
    expect(canvas.scrollLeft).toBe(0);
    view.unmount();
    expect(disconnect).toHaveBeenCalled();
  });
  it("starts with compact controls and loads only three thumbnails when the navigator opens", async () => {
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    expect(screen.queryByRole("button", { name: "Acercar" })).not.toBeInTheDocument();
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Página 1 de 5" }));
    expect(await screen.findByRole("img", { name: "Miniatura de página 3" })).toBeInTheDocument();
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(3);
    fireEvent.change(screen.getByRole("spinbutton", { name: "Ir a la página" }), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Ir" }));
    expect(await screen.findByRole("img", { name: "Capítulo 1, página 5 de 5" })).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Siguiente capítulo · Capítulo 2" })).toHaveAttribute("href", `/reader/${nextId}`);
    expect(screen.getByRole("button", { name: "Página siguiente" })).toBeDisabled();
  });

  it("keeps tall images readable and restores a fraction inside the server's saved page", async () => {
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode: "vertical" });
    vi.mocked(api.getReaderProgress).mockResolvedValue({ chapterId, seriesId, pageIndex: 0, percentage: "20.00", updatedAt: "2026-10-10T00:00:00Z" });
    saveVerticalAnchor(chapterId, { page: 1, fraction: 0.5 });
    openReader();
    const image = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    decode(image, 400, 4000);
    const viewport = screen.getByRole("region", { name: "Páginas en lectura vertical" });
    const firstSlot = viewport.querySelector("article")! as HTMLElement;
    expect(firstSlot.style.height).toBe("7692px");
    await waitFor(() => expect(viewport.scrollTop).toBe(3846));
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    fireEvent.click(screen.getByRole("button", { name: "Webtoon sin separación" }));
    expect(firstSlot.style.height).toBe("7680px");
    expect(viewport.scrollTop).toBe(3840);
  });

  it("preserves the exact reading anchor when changing width", async () => {
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode: "vertical" });
    openReader();
    const image = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    decode(image, 400, 4000);
    const viewport = screen.getByRole("region", { name: "Páginas en lectura vertical" });
    fireEvent.wheel(viewport, { deltaY: 100 });
    fireEvent.scroll(viewport, { target: { scrollTop: 3846 } });
    Object.defineProperty(viewport, "clientWidth", { configurable: true, value: 400 });
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(viewport.scrollTop).toBe(2006));
  });

  it("does not hide focused controls and lets Escape leave immersive mode", async () => {
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    vi.useFakeTimers();
    const toggle = screen.getByRole("button", { name: "Modo inmersivo" });
    fireEvent.click(toggle);
    toggle.focus();
    act(() => { vi.advanceTimersByTime(3100); });
    expect(screen.getByRole("button", { name: "Opciones de lectura" })).toBeInTheDocument();
    fireEvent.keyDown(toggle, { key: "Escape" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });

  it("hides inactive immersive controls and exposes a keyboard-accessible way to show them", async () => {
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Modo inmersivo" }));
    act(() => { vi.advanceTimersByTime(3100); });
    expect(screen.queryByRole("button", { name: "Opciones de lectura" })).not.toBeInTheDocument();
    fireEvent.mouseMove(screen.getByRole("button", { name: "Mostrar controles" }));
    expect(screen.queryByRole("button", { name: "Opciones de lectura" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Mostrar controles" }));
    expect(screen.getByRole("button", { name: "Opciones de lectura" })).toBeInTheDocument();
  });

  it("finds the next chapter across a paginated boundary", async () => {
    vi.mocked(api.getReaderChaptersPage).mockImplementation(async ({ offset }) => ({
      data: offset === 0 ? [
        ...Array.from({ length: 99 }, (_, index) => ({ chapterId: `123e4567-e89b-42d3-a456-${String(index).padStart(12, "0")}`, seriesId, displayLabel: `Anterior ${index}`, pageCount: 5 })),
        { chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 },
      ]
        : [{ chapterId: nextId, seriesId, displayLabel: "Capítulo 2", pageCount: 5 }],
      meta: { limit: 100, offset, total: 101 },
    }));
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    for (let index = 0; index < 4; index++) fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    expect(await screen.findByRole("link", { name: "Siguiente capítulo · Capítulo 2" })).toHaveAttribute("href", `/reader/${nextId}`);
    expect(api.getReaderChaptersPage).toHaveBeenCalledWith({ seriesId, offset: 100, limit: 100 });
  });

  it("falls back to one page on narrow screens without rewriting the saved double-page preference", async () => {
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode: "double" });
    vi.stubGlobal("innerWidth", 390);
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    expect(screen.queryByRole("img", { name: "Capítulo 1, página 2 de 5" })).not.toBeInTheDocument();
    expect(api.putReaderSettings).not.toHaveBeenCalled();
    vi.stubGlobal("innerWidth", 1200);
    fireEvent(window, new Event("resize"));
    expect(await screen.findByRole("img", { name: "Capítulo 1, página 2 de 5" })).toBeInTheDocument();
  });
});
