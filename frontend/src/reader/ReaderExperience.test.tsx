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
}));
const chapterId = "123e4567-e89b-42d3-a456-426614174051";
const seriesId = "123e4567-e89b-42d3-a456-426614174050";
const nextId = "223e4567-e89b-42d3-a456-426614174051";
const settings: api.ReaderSettings = { mode: "single", rtl: false, e_reader: false, preload_ahead: 2, cache_limit_mb: 16 };

function openReader() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
    <MemoryRouter initialEntries={[`/reader/${chapterId}`]}><Routes><Route path="/reader/:chapterId" element={<ReaderPage />} /></Routes></MemoryRouter>
  </QueryClientProvider>);
}
function decode(image: HTMLElement, width: number, height: number) {
  Object.defineProperty(image, "naturalWidth", { configurable: true, value: width });
  Object.defineProperty(image, "naturalHeight", { configurable: true, value: height });
  fireEvent.load(image);
}
beforeEach(() => {
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
    fireEvent.pointerDown(canvas, { pointerId: 1, pointerType: "touch", clientX: 100, clientY: 100 });
    fireEvent.pointerDown(canvas, { pointerId: 2, pointerType: "touch", clientX: 200, clientY: 100 });
    fireEvent.pointerMove(canvas, { pointerId: 2, pointerType: "touch", clientX: 300, clientY: 100 });
    expect(image.style.transform).toBe("scale(2)");
    fireEvent.pointerUp(canvas, { pointerId: 2, pointerType: "touch", clientX: 300, clientY: 100 });
    fireEvent.pointerUp(canvas, { pointerId: 1, pointerType: "touch", clientX: 100, clientY: 100 });
    expect(screen.getByRole("button", { name: "Página 1 de 5" })).toBeInTheDocument();
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
