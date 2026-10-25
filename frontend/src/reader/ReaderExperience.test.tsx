import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { ReaderPage } from "./ReaderPage";
import { readReaderAnchor, saveReaderAnchor } from "./readerPresentation";

vi.mock("../api/client", async (importOriginal) => {
  const openChapter = vi.fn();
  const getProgress = vi.fn();
  return {
    ...await importOriginal<typeof import("../api/client")>(),
    getReaderChapter: vi.fn(), getReaderChaptersPage: vi.fn(), getSeriesDetail: vi.fn(),
    getReaderPageBlob: vi.fn(), getReaderProgress: getProgress, getReaderSettings: vi.fn(),
    putReaderProgress: vi.fn(), putReaderSettings: vi.fn(),
    openOnlineReaderChapter: openChapter,
    openOnlineReader: vi.fn(async (...args: [string, boolean?]) => ({
      chapter: await openChapter(...args), progress: await getProgress(args[0]),
    })),
    getReaderDownloadSource: vi.fn(),
    getConnectorChapters: vi.fn(), registerOnlineReaderChapter: vi.fn(),
  };
});
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
  Object.defineProperty(image, "complete", { configurable: true, value: true });
  Object.defineProperty(image, "naturalWidth", { configurable: true, value: width });
  Object.defineProperty(image, "naturalHeight", { configurable: true, value: height });
  fireEvent.load(image);
}
function openAdvancedOptions() {
  const trigger = screen.getByRole("button", { name: "Opciones de lectura" });
  if (trigger.getAttribute("aria-expanded") !== "true") fireEvent.click(trigger);
  const summary = screen.getByText("Más opciones", { selector: "summary" });
  if (!summary.closest("details")!.open) fireEvent.click(summary);
}
function enableImmersive() {
  openAdvancedOptions();
  fireEvent.click(screen.getByRole("button", { name: "Modo inmersivo" }));
  fireEvent.click(screen.getByRole("button", { name: "Cerrar configuración" }));
  (document.querySelector("section[aria-labelledby='reader-title']") as HTMLElement).focus();
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

  it("keeps advanced settings collapsed and opens chapter navigation from the header", async () => {
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 });
    openReader(true);
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    const chapters = screen.getByRole("button", { name: /^Lista de capítulos/ });
    expect(chapters.closest("header")).not.toBeNull();
    fireEvent.click(chapters);
    await screen.findByRole("heading", { name: "Capítulos" });
    fireEvent.keyDown(screen.getByRole("heading", { name: "Capítulos" }), { key: "Escape" });
    expect(chapters).toHaveFocus();
    expect(chapters).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    const advanced = screen.getByText("Más opciones", { selector: "summary" }).closest("details")!;
    expect(advanced).not.toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "Página única" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ajustar al ancho" })).toBeInTheDocument();
    expect(advanced).toContainElement(screen.getByRole("button", { name: "Perfil e-reader" }));
    expect(advanced).toContainElement(screen.getByRole("button", { name: "Renovar imágenes del capítulo" }));
    openAdvancedOptions();
    expect(advanced).toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "Perfil e-reader" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Renovar imágenes del capítulo" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cerrar configuración" }));
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    expect(screen.getByText("Más opciones", { selector: "summary" }).closest("details")).not.toHaveAttribute("open");
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });

  it.each(["single", "double"] as const)("restores and saves the exact canvas position in %s mode, including a quick close", async (mode) => {
    vi.stubGlobal("innerWidth",1200);
    const width = vi.spyOn(HTMLElement.prototype,"clientWidth","get").mockReturnValue(1000);
    vi.spyOn(HTMLElement.prototype,"clientHeight","get").mockReturnValue(600);
    let contentHeight = 10000;
    vi.spyOn(HTMLElement.prototype,"getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.parentElement?.getAttribute("aria-label") === "Superficie de lectura"
        ? new DOMRect(0,0,2000,contentHeight) : new DOMRect();
    });
    let resize!: () => void;
    vi.stubGlobal("ResizeObserver",class {
      constructor(callback: () => void) { resize = callback; }
      observe() {} disconnect() {}
    });
    vi.mocked(api.getReaderSettings).mockResolvedValue({...settings,mode});
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({chapterId,seriesId,displayLabel:"Capítulo 1",pageCount:5});
    vi.mocked(api.getReaderProgress).mockResolvedValue({chapterId,seriesId,pageIndex:2,
      percentage:"50.00",updatedAt:"2026-10-18T20:00:00Z"});
    saveReaderAnchor(chapterId,{page:3,fraction:0.4,horizontal:0.2},mode);
    const view = openReader(true);
    const first = await screen.findByRole("img",{name:"Capítulo 1, página 3 de 5"});
    const canvas = screen.getByRole("region",{name:"Superficie de lectura"});
    expect(canvas.scrollTop).toBe(0);
    decode(first,700,14000);
    if (mode === "double") {
      expect(canvas.scrollTop).toBe(0);
      decode(await screen.findByRole("img",{name:"Capítulo 1, página 4 de 5"}),700,16000);
    }
    await waitFor(() => expect(canvas.scrollTop).toBe(4000));
    expect(canvas.scrollLeft).toBe(400);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    fireEvent.scroll(canvas,{target:{scrollTop:6500,scrollLeft:300}});
    fireEvent.scroll(canvas,{target:{scrollTop:0,scrollLeft:0}});
    fireEvent(window,new Event("pagehide"));
    expect(readReaderAnchor(chapterId,3,mode)).toEqual({page:3,fraction:0,horizontal:0});
    fireEvent.scroll(canvas,{target:{scrollTop:6500,scrollLeft:300}});
    fireEvent(window,new Event("pagehide"));
    expect(readReaderAnchor(chapterId,3,mode)).toEqual({page:3,fraction:0.65,horizontal:0.15});
    view.unmount();
    const reopened = openReader(true);
    decode(await screen.findByRole("img",{name:"Capítulo 1, página 3 de 5"}),700,14000);
    if (mode === "double") decode(await screen.findByRole("img",{name:"Capítulo 1, página 4 de 5"}),700,16000);
    const restored = screen.getByRole("region",{name:"Superficie de lectura"});
    await waitFor(() => expect(restored.scrollTop).toBe(6500));
    expect(restored.scrollLeft).toBe(300);
    contentHeight = 20000;
    width.mockReturnValue(2000);
    act(resize);
    await waitFor(() => expect(restored.scrollTop).toBe(13000));
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    reopened.unmount();
  });

  it("restores a mode's own bookmark after its already cached image loads", async () => {
    vi.stubGlobal("innerWidth",1200);
    vi.spyOn(HTMLElement.prototype,"clientWidth","get").mockReturnValue(1000);
    vi.spyOn(HTMLElement.prototype,"clientHeight","get").mockReturnValue(600);
    vi.spyOn(HTMLElement.prototype,"getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.parentElement?.getAttribute("aria-label") === "Superficie de lectura"
        ? new DOMRect(0,0,1000,10000) : new DOMRect();
    });
    vi.mocked(api.getReaderSettings).mockResolvedValue({...settings,mode:"double"});
    vi.mocked(api.getReaderProgress).mockResolvedValue({chapterId,seriesId,pageIndex:0,
      percentage:"0.00",updatedAt:"2026-10-18T20:00:00Z"});
    saveReaderAnchor(chapterId,{page:1,fraction:0.6,horizontal:0},"single");
    const view = openReader();
    decode(await screen.findByRole("img",{name:"Capítulo 1, página 1 de 5"}),700,14000);
    decode(await screen.findByRole("img",{name:"Capítulo 1, página 2 de 5"}),700,16000);
    fireEvent.click(screen.getByRole("button",{name:"Opciones de lectura"}));
    fireEvent.click(screen.getByRole("button",{name:"Página única"}));
    const cached = await screen.findByRole("img",{name:"Capítulo 1, página 1 de 5"});
    expect(screen.getByRole("region",{name:"Superficie de lectura"}).scrollTop).toBe(0);
    decode(cached,700,14000);
    await waitFor(() => expect(screen.getByRole("region",{name:"Superficie de lectura"}).scrollTop).toBe(6000));
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    view.unmount();
  });

  it("ignores a canvas position from a different server page", async () => {
    vi.spyOn(HTMLElement.prototype,"getBoundingClientRect").mockReturnValue(new DOMRect(0,0,1000,10000));
    vi.mocked(api.getReaderProgress).mockResolvedValue({chapterId,seriesId,pageIndex:3,
      percentage:"75.00",updatedAt:"2026-10-18T20:00:00Z"});
    saveReaderAnchor(chapterId,{page:3,fraction:0.8,horizontal:0},"single");
    const view = openReader();
    decode(await screen.findByRole("img",{name:"Capítulo 1, página 4 de 5"}),700,14000);
    expect(screen.getByRole("region",{name:"Superficie de lectura"}).scrollTop).toBe(0);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    view.unmount();
  });

  it("loads the current image before spending bandwidth on its neighbours", async () => {
    let release!: (blob: Blob) => void;
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 });
    vi.mocked(api.getReaderPageBlob).mockImplementation(async (_id, number) => number === 1
      ? new Promise<Blob>((resolve) => { release = resolve; }) : new Blob(["page"]));
    const view = openReader(true);
    await waitFor(() => expect(api.getReaderPageBlob).toHaveBeenCalledTimes(1));
    expect(api.getReaderPageBlob).toHaveBeenLastCalledWith(chapterId, 1, expect.any(AbortSignal));
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    await act(async () => { release(new Blob(["page"])); });
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    await waitFor(() => expect(api.getReaderPageBlob).toHaveBeenCalledTimes(3));
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    view.unmount();
  });

  it("starts both visible pages together in double-page mode before requesting any preload", async () => {
    const releases = new Map<number, (blob: Blob) => void>();
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode: "double" });
    vi.stubGlobal("innerWidth", 1200);
    vi.mocked(api.getReaderPageBlob).mockImplementation(async (_id, number) => number <= 2
      ? new Promise<Blob>((resolve) => { releases.set(number, resolve); }) : new Blob(["page"]));
    const view = openReader();
    await waitFor(() => expect(api.getReaderPageBlob).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.getReaderPageBlob).mock.calls.map(([, number]) => number)).toEqual([1, 2]);
    await act(async () => { releases.get(1)!(new Blob(["page"])); });
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(2);
    await act(async () => { releases.get(2)!(new Blob(["page"])); });
    await waitFor(() => expect(api.getReaderPageBlob).toHaveBeenCalledTimes(4));
    view.unmount();
  });

  it("keeps a tall single page readable even when height fitting is selected", async () => {
    let resize!: () => void;
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: () => void) { resize = callback; }
      observe() {} disconnect() {}
    });
    const view = openReader();
    const image = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    const canvas = screen.getByRole("region", { name: "Superficie de lectura" });
    canvas.style.padding = "0px";
    Object.defineProperty(canvas, "clientWidth", { configurable: true, value: 1000 });
    Object.defineProperty(canvas, "clientHeight", { configurable: true, value: 600 });
    act(resize);
    decode(image, 700, 14000);
    expect(image.style.width).toBe("1000px");
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    fireEvent.click(screen.getByRole("button", { name: "Ajustar al alto" }));
    expect(image.style.width).toBe("1000px");
    view.unmount();
  });

  it.each(["single", "double"] as const)("keeps long strips readable with a saved height fit in %s mode", async (mode) => {
    localStorage.setItem("yomira.reader.presentation.v1", JSON.stringify({ fit: "height", zoom: 1 }));
    vi.stubGlobal("innerWidth", 1200);
    vi.spyOn(HTMLElement.prototype,"clientWidth","get").mockReturnValue(1000);
    vi.spyOn(HTMLElement.prototype,"clientHeight","get").mockReturnValue(600);
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode });
    let resize!: () => void;
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: () => void) { resize = callback; }
      observe() {} disconnect() {}
    });
    const view = openReader();
    const first = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    const canvas = screen.getByRole("region", { name: "Superficie de lectura" });
    canvas.style.padding = "0px";
    Object.defineProperty(canvas,"clientWidth",{ configurable:true,value:1000 });
    Object.defineProperty(canvas,"clientHeight",{ configurable:true,value:600 });
    act(resize);
    decode(first,700,19678);
    expect(first.style.width).toBe(mode === "double" ? "499px" : "1000px");
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    expect(screen.getByRole("button",{ name:"Ajustar al alto" })).toHaveAttribute("aria-pressed","true");
    if (mode === "double") {
      const second = await screen.findByRole("img",{ name:"Capítulo 1, página 2 de 5" });
      decode(second,700,19000);
      expect(second.style.width).toBe("499px");
    }
    fireEvent.click(screen.getByRole("button",{ name: mode === "single" ? "Doble página" : "Página única" }));
    await waitFor(() => expect(screen.getByRole("img",{ name:"Capítulo 1, página 1 de 5" }).style.width).toBe(mode === "single" ? "499px" : "1000px"));
    view.unmount();
  });

  it("restores saved progress supplied with the online opening response", async () => {
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 8 });
    vi.mocked(api.getReaderProgress).mockResolvedValue({ chapterId, seriesId, pageIndex: 5,
      percentage: "71.43", updatedAt: "2026-10-10T00:00:00Z" });
    openReader(true);
    await screen.findByRole("img", { name: "Capítulo 1, página 6 de 8" });
    expect(api.openOnlineReader).toHaveBeenCalledTimes(1);
    expect(api.openOnlineReader).toHaveBeenLastCalledWith(chapterId);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });

  it("keeps the current online page readable when only a prefetched page returns 404", async () => {
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 });
    vi.mocked(api.getReaderPageBlob).mockImplementation(async (_id, number) => {
      if (number === 2) throw new api.ApiError({ status: 404, code: "API_REQUEST_REJECTED",
        message: "Session absent", traceId: "preload-gone", retryable: false });
      return new Blob(["page"], { type: "image/jpeg" });
    });
    openReader(true);
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    await waitFor(() => expect(api.getReaderPageBlob).toHaveBeenCalledTimes(3));
    expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(1);
    vi.mocked(api.getReaderPageBlob).mockResolvedValue(new Blob(["page"], { type: "image/jpeg" }));
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    await screen.findByRole("img", { name: "Capítulo 1, página 2 de 5" });
    expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(2);
    expect(api.openOnlineReaderChapter).toHaveBeenLastCalledWith(chapterId, false);
  });

  it("does not return to the reader when automatic reconnection completes after leaving", async () => {
    let complete!: (chapter: api.ReaderChapter) => void;
    const chapter = { chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 };
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValueOnce(chapter)
      .mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    vi.mocked(api.getReaderPageBlob).mockRejectedValue(new api.ApiError({ status: 404,
      code: "API_REQUEST_REJECTED", message: "Session absent", traceId: "leave-gone", retryable: false }));
    openReader(true);
    await screen.findByText("Restableciendo la lectura en tu página actual…");
    fireEvent.click(screen.getByRole("link", { name: "Volver a la serie" }));
    const requests = vi.mocked(api.getReaderPageBlob).mock.calls.length;
    await act(async () => { complete(chapter); });
    expect(screen.getByRole("heading", { name: "Ficha de la serie" })).toBeInTheDocument();
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(requests);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });

  it.each(["single", "vertical", "double"] as const)("recovers a lost online session once in %s mode without renewing images or losing page 6", async (mode) => {
    let generation = 0;
    let complete!: (chapter: api.ReaderChapter) => void;
    const chapter = { chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 8 };
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode });
    vi.mocked(api.getReaderProgress).mockResolvedValue({ chapterId, seriesId, pageIndex: 5,
      percentage: "71.43", updatedAt: "2026-10-10T00:00:00Z" });
    vi.mocked(api.openOnlineReaderChapter).mockImplementation(async () => {
      generation++;
      return generation === 1 ? chapter : new Promise((resolve) => { complete = resolve; });
    });
    vi.mocked(api.getReaderPageBlob).mockImplementation(async () => {
      if (generation === 1) throw new api.ApiError({ status: 404, code: "API_REQUEST_REJECTED",
        message: "Session absent after restart", traceId: "lost-session", retryable: false });
      return new Blob(["page"], { type: "image/jpeg" });
    });
    openReader(true, 0, true);
    await waitFor(() => expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(2));
    expect(api.openOnlineReaderChapter).toHaveBeenLastCalledWith(chapterId, false);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    await act(async () => { complete(chapter); });
    await screen.findByRole("img", { name: "Capítulo 1, página 6 de 8" });
    expect(screen.queryByRole("button", { name: /Reintentar página/ })).not.toBeInTheDocument();
    expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(2);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });

  it("does not loop on a persistent online 404 and leaves manual renewal available", async () => {
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 });
    vi.mocked(api.getReaderPageBlob).mockRejectedValue(new api.ApiError({ status: 404,
      code: "API_REQUEST_REJECTED", message: "Missing page", traceId: "persistent", retryable: false }));
    openReader(true, 0, true);
    await screen.findByRole("heading", { name: "No se pudo recuperar la página" });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
    expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Reintentar página" }));
    await waitFor(() => expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(3));
    expect(api.openOnlineReaderChapter).toHaveBeenLastCalledWith(chapterId, true);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });

  it.each(["unavailable", "changed"])("stops automatic session recovery when the source is %s", async (failure) => {
    const chapter = { chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 };
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValueOnce(chapter);
    if (failure === "unavailable") vi.mocked(api.openOnlineReaderChapter).mockRejectedValue(new Error("Source unavailable"));
    else vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({ ...chapter, pageCount: 2 });
    vi.mocked(api.getReaderPageBlob).mockRejectedValue(new api.ApiError({ status: 404,
      code: "API_REQUEST_REJECTED", message: "Session absent", traceId: "gone", retryable: false }));
    openReader(true, 0, true);
    await screen.findByRole("heading", { name: "No se pudo abrir el capítulo" });
    if (failure === "changed") expect(screen.getByText(/La fuente cambió el capítulo/)).toBeInTheDocument();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
    expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(2);
    expect(api.openOnlineReaderChapter).toHaveBeenLastCalledWith(chapterId, false);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });

  it("recovers a session lost during progress saving and saves again only after decoding the recovered page", async () => {
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 });
    vi.mocked(api.putReaderProgress).mockRejectedValueOnce(new api.ApiError({ status: 404,
      code: "API_REQUEST_REJECTED", message: "Session disappeared", traceId: "save-gone", retryable: false }));
    openReader(true);
    decode(await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" }), 400, 600);
    await waitFor(() => expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(2));
    expect(api.openOnlineReaderChapter).toHaveBeenLastCalledWith(chapterId, false);
    const recovered = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    expect(api.putReaderProgress).toHaveBeenCalledTimes(1);
    decode(recovered, 400, 600);
    await waitFor(() => expect(api.putReaderProgress).toHaveBeenCalledTimes(2));
    expect(api.putReaderProgress).toHaveBeenLastCalledWith(chapterId, 0);
  });

  it("keeps local missing pages manual without opening an online source", async () => {
    vi.mocked(api.getReaderPageBlob).mockRejectedValue(new api.ApiError({ status: 404,
      code: "API_REQUEST_REJECTED", message: "Local page absent", traceId: "local", retryable: false }));
    openReader();
    await screen.findByRole("heading", { name: "Página no encontrada" });
    expect(api.openOnlineReaderChapter).not.toHaveBeenCalled();
  });

  it("renews an online chapter from settings while preserving the current page", async () => {
    let complete!: (chapter: api.ReaderChapter) => void;
    vi.mocked(api.openOnlineReaderChapter)
      .mockResolvedValueOnce({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 })
      .mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    vi.mocked(api.getReaderProgress).mockResolvedValue({ chapterId, seriesId, pageIndex: 2,
      percentage: "50.00", updatedAt: "2026-10-10T00:00:00Z" });
    openReader(true);
    await screen.findByRole("img", { name: "Capítulo 1, página 3 de 5" });
    openAdvancedOptions();
    fireEvent.click(screen.getByRole("button", { name: "Renovar imágenes del capítulo" }));
    await screen.findByText("Cargando información del capítulo…");
    expect(api.openOnlineReaderChapter).toHaveBeenCalledTimes(2);
    expect(api.openOnlineReaderChapter).toHaveBeenLastCalledWith(chapterId, true);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
    await act(async () => { complete({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 }); });
    const image = await screen.findByRole("img", { name: "Capítulo 1, página 3 de 5" });
    expect(api.putReaderProgress).not.toHaveBeenCalled();
    await act(async () => { decode(image, 400, 600); });
    expect(screen.getByRole("button", { name: "Página 3 de 5" })).toBeInTheDocument();
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });

  it("reopens an online-only chapter from a plain resume URL and restores the saved page", async () => {
    vi.mocked(api.getReaderChapter).mockRejectedValueOnce(new api.ApiError({status: 404,
      code: "API_REQUEST_REJECTED", message: "Unavailable locally", traceId: "resume-404", retryable: false}));
    vi.mocked(api.getReaderDownloadSource).mockResolvedValue({connectorId: "yomira.mock", remoteSeriesId: "fixture", chapterRemoteId: "chapter-1"});
    vi.mocked(api.getConnectorChapters).mockResolvedValue({data:[],meta:{connectorId:"yomira.mock",remoteSeriesId:"fixture"}});
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5});
    vi.mocked(api.getReaderProgress).mockResolvedValue({chapterId, seriesId, pageIndex: 2, percentage: "60.00", updatedAt: "2026-10-10T00:00:00Z"});
    openReader();
    await screen.findByRole("img", {name: "Capítulo 1, página 3 de 5"});
    expect(api.openOnlineReaderChapter).toHaveBeenCalledExactlyOnceWith(chapterId);
    expect(api.getReaderDownloadSource).toHaveBeenCalledExactlyOnceWith(chapterId);
    expect(api.getReaderPageBlob).not.toHaveBeenCalledWith(chapterId, 1);
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });
  it.each([404, 503])("does not invent an online source when a plain entry fails with %s", async (status) => {
    vi.mocked(api.getReaderChapter).mockRejectedValue(new api.ApiError({status,
      code: "API_REQUEST_REJECTED", message: "Unavailable", traceId: "entry-failure", retryable: false}));
    openReader();
    await screen.findByRole("heading", {name: status === 404 ? "Capítulo no disponible" : "No se pudo abrir el capítulo"});
    expect(api.getReaderDownloadSource).toHaveBeenCalledTimes(status === 404 ? 1 : 0);
    expect(api.openOnlineReaderChapter).not.toHaveBeenCalled();
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });
  it.each(["single", "double"] as const)("preloads upcoming pages in %s mode and reuses them on navigation", async (mode) => {
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode });
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    await waitFor(() => expect(api.getReaderPageBlob).toHaveBeenCalledTimes(mode === "double" ? 4 : 3));
    const target = mode === "double" ? 3 : 2;
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    await screen.findByRole("img", { name: `Capítulo 1, página ${target} de 5` });
    expect(vi.mocked(api.getReaderPageBlob).mock.calls.filter(([, number]) => number === target)).toHaveLength(1);
  });
  it("requests the restored vertical page before any neighbour", async () => {
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode: "vertical" });
    vi.mocked(api.getReaderProgress).mockResolvedValue({ chapterId, seriesId, pageIndex: 2,
      percentage: "50.00", updatedAt: "2026-10-10T00:00:00Z" });
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 3 de 5" });
    expect(vi.mocked(api.getReaderPageBlob).mock.calls.map(([, number]) => number)).toEqual([3, 4, 5, 2]);
  });
  it("waits for saved progress and preloads only the restored page window", async () => {
    let restore!: (value: Awaited<ReturnType<typeof api.getReaderProgress>>) => void;
    vi.mocked(api.getReaderProgress).mockImplementation(() => new Promise((resolve) => { restore = resolve; }));
    openReader();
    await screen.findByRole("heading", { name: "Capítulo 1" });
    expect(api.getReaderPageBlob).not.toHaveBeenCalled();
    await act(async () => restore({ chapterId, seriesId, pageIndex: 2, percentage: "60.00", updatedAt: "2026-10-10T00:00:00Z" }));
    await screen.findByRole("img", { name: "Capítulo 1, página 3 de 5" });
    expect(vi.mocked(api.getReaderPageBlob).mock.calls.map(([, number]) => number)).toEqual([3, 4, 5]);
  });
  it("keeps an online page readable when preloading fails without saving the failed next page", async () => {
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 });
    vi.mocked(api.getReaderPageBlob).mockImplementation(async (_id, number) => {
      if (number === 2) throw new Error("preload unavailable");
      return new Blob(["page"], { type: "image/jpeg" });
    });
    openReader(true);
    const image = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    expect(screen.queryByText("No se pudo cargar la página")).not.toBeInTheDocument();
    decode(image, 400, 600);
    await waitFor(() => expect(api.putReaderProgress).toHaveBeenCalledWith(chapterId, 0));
    vi.mocked(api.putReaderProgress).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    expect(await screen.findByRole("heading", { name: "No se pudo cargar la página" })).toBeInTheDocument();
    expect(api.putReaderProgress).not.toHaveBeenCalled();
  });
  it("closes settings with Escape without leaving immersive reading and returns focus", async () => {
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    enableImmersive();
    const trigger = screen.getByRole("button", { name: "Opciones de lectura" });
    fireEvent.click(trigger);
    expect(screen.getByRole("button", { name: "Cerrar configuración" })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(trigger).toHaveFocus();
    expect(screen.getByRole("region", { name: "Superficie de lectura" }).closest("section")).toHaveClass(/immersive/);
    expect(screen.queryByRole("button", { name: "Cerrar configuración" })).not.toBeInTheDocument();
  });
  it("allows clearing the page jump, rejects invalid values and returns focus after jumping", async () => {
    openReader();
    const trigger = await screen.findByRole("button", { name: "Página 1 de 5" });
    fireEvent.click(trigger);
    const input = screen.getByRole("spinbutton", { name: "Ir a la página" });
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: "" } });
    expect(input).toHaveValue(null);
    expect(screen.getByRole("button", { name: "Ir" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "999" } });
    fireEvent.submit(input.closest("form")!);
    expect(screen.getByRole("button", { name: "Página 1 de 5" })).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: "Ir" }));
    expect(await screen.findByRole("button", { name: "Página 4 de 5" })).toHaveFocus();
  });
  it("remembers background and brightness and preserves the unfiltered e-reader profile", async () => {
    const view = openReader();
    const image = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    fireEvent.click(screen.getByRole("button", { name: "Sepia" }));
    fireEvent.change(screen.getByRole("slider", { name: "Brillo de páginas" }), { target: { value: "70" } });
    expect(image.closest("section")).toHaveStyle({ "--reader-background": "#e8dcc4", "--reader-brightness": "0.7" });
    expect(screen.getByRole("img", { name: "Vista previa de lectura" })).toHaveAttribute("data-theme", "sepia");
    view.unmount();
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    expect(screen.getByRole("button", { name: "Sepia" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("slider", { name: "Brillo de páginas" })).toHaveValue("70");
    openAdvancedOptions();
    fireEvent.click(screen.getByRole("button", { name: "Perfil e-reader" }));
    expect(screen.getByRole("slider", { name: "Brillo de páginas" })).toBeDisabled();
    expect(screen.getByRole("slider", { name: "Brillo de páginas" })).toHaveValue("100");
    expect(screen.getByRole("img", { name: "Vista previa de lectura" })).toHaveAttribute("data-theme", "light");
  });
  it("selects continuous webtoon or separated vertical reading while keeping the saved page and preference", async () => {
    vi.mocked(api.getReaderProgress).mockResolvedValue({ chapterId, seriesId, pageIndex: 2, percentage: "60.00", updatedAt: "2026-10-10T00:00:00Z" });
    const view = openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 3 de 5" });
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    expect(screen.getByRole("img", { name: "Vista previa de lectura" })).toHaveAttribute("data-mode", "single");

    fireEvent.click(screen.getByRole("button", { name: "Webtoon continuo" }));
    await waitFor(() => expect(api.putReaderSettings).toHaveBeenLastCalledWith({ ...settings, mode: "vertical" }));
    expect(screen.getByRole("button", { name: "Webtoon continuo" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Vertical" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("img", { name: "Vista previa de lectura" })).toHaveAttribute("data-mode", "webtoon");
    expect(screen.getByRole("button", { name: "Página 3 de 5" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Vertical" }));
    expect(screen.getByRole("button", { name: "Vertical" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Webtoon continuo" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("img", { name: "Vista previa de lectura" })).toHaveAttribute("data-mode", "vertical");
    expect(JSON.parse(localStorage.getItem("yomira.reader.presentation.v1")!).continuous).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Webtoon continuo" }));
    view.unmount();
    vi.mocked(api.getReaderSettings).mockResolvedValue({ ...settings, mode: "vertical" });
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 3 de 5" });
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    expect(screen.getByRole("button", { name: "Webtoon continuo" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("img", { name: "Vista previa de lectura" })).toHaveAttribute("data-mode", "webtoon");
    expect(screen.getByRole("button", { name: "Página 3 de 5" })).toBeInTheDocument();
  });
  it("uses the selected shortcut tile to toggle manual controls and remembers it on reopening", async () => {
    const view = openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    openAdvancedOptions();
    fireEvent.click(screen.getByRole("button", { name: "Manual" }));
    fireEvent.click(screen.getByRole("button", { name: "C" }));
    expect(screen.getByRole("button", { name: "C" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "H" })).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByRole("button", { name: "Cerrar configuración" }));
    const reader = screen.getByRole("region", { name: "Superficie de lectura" });
    fireEvent.keyDown(reader, { key: "h" });
    expect(screen.getByRole("button", { name: "Opciones de lectura" })).toBeInTheDocument();
    fireEvent.keyDown(reader, { key: "c" });
    expect(screen.queryByRole("button", { name: "Opciones de lectura" })).not.toBeInTheDocument();

    view.unmount();
    openReader();
    fireEvent.click(await screen.findByRole("button", { name: "Mostrar controles" }));
    openAdvancedOptions();
    expect(screen.getByRole("button", { name: "Manual" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "C" })).toHaveAttribute("aria-pressed", "true");
  });
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
    openAdvancedOptions();
    fireEvent.click(screen.getByRole("button", {name: "Manual"}));
    fireEvent.keyDown(screen.getByRole("slider", {name: "Brillo de páginas"}), {key: "h"});
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
    openAdvancedOptions();
    expect(screen.getByRole("button", {name: "Manual"})).toHaveAttribute("aria-pressed", "true");
  });
  it("reveals automatic controls only near the edges during vertical reading and cleans up the timer", async () => {
    vi.mocked(api.getReaderSettings).mockResolvedValue({...settings, mode: "vertical"});
    const view = openReader();
    await screen.findByRole("img", {name: "Capítulo 1, página 1 de 5"});
    const viewport = screen.getByRole("region", {name: "Páginas en lectura vertical"});
    const root = viewport.closest("section")!;
    vi.spyOn(root, "getBoundingClientRect").mockReturnValue({top: 0, bottom: 800} as DOMRect);
    vi.useFakeTimers();
    enableImmersive();
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
    openAdvancedOptions();
    fireEvent.click(screen.getByRole("checkbox", {name: "Barra de progreso"}));
    expect(screen.queryByRole("slider", { name: "Progreso del capítulo" })).not.toBeInTheDocument();
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
    expect(api.openOnlineReaderChapter).toHaveBeenLastCalledWith(chapterId, true);
    expect(vi.mocked(api.getReaderPageBlob).mock.calls.filter(([, number]) => number === 1)).toHaveLength(1);
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
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(requestsBeforeRetry + 3);
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
    expect(vi.mocked(api.getReaderPageBlob).mock.calls.filter(([, number]) => number === 1)).toHaveLength(2);
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
    fireEvent.click(screen.getByRole("link", { name: "Volver a la serie" }));
    await act(async () => { complete({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 }); });
    expect(screen.getByRole("heading", { name: "Ficha de la serie" })).toBeInTheDocument();
    expect(api.getReaderPageBlob).toHaveBeenCalledTimes(1);
    expect(api.getReaderPageBlob).toHaveBeenLastCalledWith(chapterId, 1, expect.any(AbortSignal));
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
  it("navigates a unique current catalog label when a saved source link has expired", async () => {
    onlineCatalog();
    vi.mocked(api.getReaderDownloadSource).mockImplementation(async (id) => ({ connectorId: "yomira.mock",
      remoteSeriesId: "series", chapterRemoteId: id === chapterId ? "expired-link" : "remote-2" }));
    openReader(true);
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 2" });
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    fireEvent.click(await screen.findByRole("button", { name: "Siguiente capítulo · Capítulo 2" }));
    await screen.findByRole("img", { name: "Capítulo 2, página 1 de 2" });
    expect(api.registerOnlineReaderChapter).toHaveBeenCalledExactlyOnceWith("yomira.mock", "series", "remote-2");
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
    fireEvent.click(screen.getByRole("link", { name: "Volver a la serie" }));
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
    enableImmersive();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    await screen.findByText(/Puedes seguir leyendo tu copia local/);
    vi.useFakeTimers();
    act(() => { vi.advanceTimersByTime(3100); });
    expect(screen.getByRole("heading", { name: "Descargar capítulos" })).toBeInTheDocument();
  });
  it("keeps the chapter dropdown visible in immersive mode and closes it with Escape", async () => {
    openReader();
    await screen.findByRole("heading", { name: "Capítulo 1" });
    enableImmersive();
    fireEvent.click(screen.getByRole("button", { name: /^Lista de capítulos/ }));
    await screen.findByRole("heading", { name: "Capítulos" });
    vi.useFakeTimers();
    act(() => { vi.advanceTimersByTime(3100); });
    expect(screen.getByRole("heading", { name: "Capítulos" })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("heading", { name: "Capítulos" }), { key: "Escape" });
    expect(screen.queryByRole("heading", { name: "Capítulos" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Lista de capítulos/ })).toHaveAttribute("aria-expanded", "false");
  });
  it("opens online through the existing reader and waits for a displayed page before saving it", async () => {
    vi.mocked(api.openOnlineReaderChapter).mockResolvedValue({ chapterId, seriesId, displayLabel: "Capítulo 1", pageCount: 5 });
    openReader(true);
    const first = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    expect(api.openOnlineReaderChapter).toHaveBeenCalledWith(chapterId);
    expect(api.getReaderChapter).not.toHaveBeenCalled();
    expect(screen.queryByText("Online · descarga opcional")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
    expect(screen.getByText("Online · descarga opcional")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("heading", { name: "Descargar capítulos" }), { key: "Escape" });
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
    await waitFor(() => expect(api.getReaderPageBlob).toHaveBeenCalledTimes(3));
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
    saveReaderAnchor(chapterId, { page: 1, fraction: 0.5 });
    openReader();
    const image = await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    decode(image, 400, 4000);
    const viewport = screen.getByRole("region", { name: "Páginas en lectura vertical" });
    const firstSlot = viewport.querySelector("article")! as HTMLElement;
    expect(firstSlot.style.height).toBe("7692px");
    await waitFor(() => expect(viewport.scrollTop).toBe(3846));
    fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
    fireEvent.click(screen.getByRole("button", { name: "Webtoon continuo" }));
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

  it("does not hide focused controls and lets Escape leave immersive mode outside panels", async () => {
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    vi.useFakeTimers();
    enableImmersive();
    const trigger = screen.getByRole("button", { name: "Opciones de lectura" });
    trigger.focus();
    act(() => { vi.advanceTimersByTime(3100); });
    expect(screen.getByRole("button", { name: "Opciones de lectura" })).toBeInTheDocument();
    fireEvent.keyDown(trigger, { key: "Escape" });
    expect(screen.getByRole("region", { name: "Superficie de lectura" }).closest("section")).not.toHaveClass(/immersive/);
  });

  it("hides inactive immersive controls and exposes a keyboard-accessible way to show them", async () => {
    openReader();
    await screen.findByRole("img", { name: "Capítulo 1, página 1 de 5" });
    vi.useFakeTimers();
    enableImmersive();
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
