vi.mock("../api/readerLibrary", async () => (await import("./readerLibrary.test-support")).readerLibraryFixture());
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getRecentReaderProgress } from "../api/client";
import { ReaderSavedPanel } from "./ReaderSavedPanel";
import { readReaderLibrary, saveReaderBookmark } from "./readerLibrary";

vi.mock("../api/client", async (original) => ({ ...await original<typeof import("../api/client")>(), getRecentReaderProgress: vi.fn() }));
const seriesId = "123e4567-e89b-42d3-a456-426614174050";
const chapterId = "123e4567-e89b-42d3-a456-426614174051";
const chapter = { seriesId, chapterId, displayLabel: "Capítulo 1", pageCount: 10 };
function open(canBookmark = true) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
    <MemoryRouter><ReaderSavedPanel chapter={chapter} page={3} canBookmark={canBookmark} onPage={vi.fn()} onClose={vi.fn()} /></MemoryRouter>
  </QueryClientProvider>);
}
beforeEach(() => { localStorage.clear(); vi.mocked(getRecentReaderProgress).mockReset().mockResolvedValue({ data: [], meta: { limit: 20 } }); });
afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); });

describe("saved reader panel", () => {
  it("saves and removes an explicit marker without querying history", async () => {
    open();
    await waitFor(() => expect(screen.getByRole("button", { name: "Marcar página 3" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Marcar página 3" }));
    await waitFor(() => expect(readReaderLibrary().bookmarks).toMatchObject([{ chapterId, pageIndex: 2 }]));
    await waitFor(() => expect(screen.getByRole("button", { name: "Página marcada" })).toBeDisabled());
    await waitFor(() => expect(screen.getByRole("button", { name: "Quitar marcador: Capítulo 1 · Página 3" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Quitar marcador: Capítulo 1 · Página 3" }));
    await waitFor(() => expect(readReaderLibrary().bookmarks).toEqual([]));
    expect(getRecentReaderProgress).not.toHaveBeenCalled();
  });
  it("waits for the visible page and fetches history only on request", async () => {
    open(false);
    expect(screen.getByRole("button", { name: "Marcar página 3" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Historial" }));
    await screen.findByText("No hay capítulos en el historial.");
    expect(getRecentReaderProgress).toHaveBeenCalledExactlyOnceWith(20);
  });
  it("reports storage failure and preserves existing markers", async () => {
    saveReaderBookmark({ seriesId, chapterId, pageIndex: 0, chapterLabel: "Capítulo 1", createdAt: new Date().toISOString() });
    const original = localStorage.getItem("yomira.reader.library.v1");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Full"); });
    open();
    await waitFor(() => expect(screen.getByRole("button", { name: "Marcar página 3" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Marcar página 3" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudieron guardar");
    expect(localStorage.getItem("yomira.reader.library.v1")).toBe(original);
  });
  it("does not navigate a saved page that disappeared from this chapter", () => {
    saveReaderBookmark({ seriesId, chapterId, pageIndex: 20, chapterLabel: "Capítulo 1", createdAt: new Date().toISOString() });
    open();
    expect(screen.getByRole("button", { name: /Página 21 · No disponible/ })).toBeDisabled();
  });
});
