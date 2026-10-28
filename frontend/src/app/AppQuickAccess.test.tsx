import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { AppQuickAccess } from "./AppQuickAccess";
import { useAppearanceStore } from "./appearanceStore";

vi.mock("../api/client", async (original) => ({ ...await original<typeof import("../api/client")>(),
  getLibraryPage: vi.fn(), getRecentReaderProgress: vi.fn(), getSystemReadiness: vi.fn() }));
const seriesId = "123e4567-e89b-42d3-a456-426614174050";
const chapterId = "123e4567-e89b-42d3-a456-426614174051";
const progress = { seriesId, chapterId, seriesDisplayTitle: "Obra reciente", chapterDisplayLabel: "Capítulo 3", pageIndex: 4, percentage: "25.00", updatedAt: "2026-10-26T00:00:00Z" };
function Harness() {
  const [open, setOpen] = useState(false); const location = useLocation();
  return <><AppQuickAccess open={open} onOpen={() => setOpen(true)} onClose={() => setOpen(false)}
    destinations={[{ label: "Inicio", path: "/" }, { label: "Configuración", path: "/settings" }, { label: "Biblioteca", path: "/library" }]} />
    <output aria-label="Destino">{location.pathname}{location.search}</output></>;
}
function setup() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}><MemoryRouter><Harness /></MemoryRouter></QueryClientProvider>);
}
beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function (this: HTMLDialogElement) { this.removeAttribute("open"); } });
  vi.mocked(api.getSystemReadiness).mockResolvedValue({ status: "ready" });
  vi.mocked(api.getRecentReaderProgress).mockResolvedValue({ data: [progress], meta: { limit: 4 } });
  vi.mocked(api.getLibraryPage).mockReset().mockResolvedValue({ data: [], meta: { limit: 6, offset: 0, total: 0 } });
  useAppearanceStore.setState({ theme: "tinta", intensity: 35, enabled: true, simpleGraphics: false });
});
afterEach(() => { vi.restoreAllMocks(); useAppearanceStore.setState({ theme: "tinta", intensity: 60 }); });
describe("real quick access from the final design", () => {
  it("opens with Ctrl K, focuses the command input, and restores focus when closed", async () => {
    setup(); const trigger = screen.getByRole("button", { name: "Abrir paleta de comandos" }); trigger.focus();
    await screen.findByRole("link", { name: "Continuar leyendo Obra reciente" });
    expect(api.getLibraryPage).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(screen.getByRole("combobox", { name: "Comando" })).toHaveFocus();
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { bubbles: true }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); expect(trigger).toHaveFocus();
  });
  it("matches accented screen names and opens the selected destination with Enter", () => {
    setup(); fireEvent.click(screen.getByRole("button", { name: "Abrir paleta de comandos" }));
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "configuracion" } });
    expect(screen.getByRole("option", { name: /Ir a Configuración/ })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByLabelText("Destino")).toHaveTextContent("/settings"); expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("searches the full real library only after typing and opens its work", async () => {
    vi.mocked(api.getLibraryPage).mockResolvedValue({ data: [{ seriesId, displayTitle: "Obra encontrada" } as api.LibrarySeriesSummary], meta: { limit: 6, offset: 0, total: 1 } });
    setup(); fireEvent.click(screen.getByRole("button", { name: "Abrir paleta de comandos" }));
    expect(api.getLibraryPage).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "Obra" } });
    fireEvent.click(await screen.findByRole("option", { name: /Obra encontrada/ }));
    expect(api.getLibraryPage).toHaveBeenCalledWith({ query: "Obra", limit: 6, offset: 0, sort: "title" });
    expect(screen.getByLabelText("Destino")).toHaveTextContent(`/series/${seriesId}`);
  });
  it("shows a search failure and keeps the real source search available with an encoded query", async () => {
    vi.mocked(api.getLibraryPage).mockRejectedValue(new Error("Offline"));
    setup(); fireEvent.click(screen.getByRole("button", { name: "Abrir paleta de comandos" }));
    const query = "Manga & cómic";
    fireEvent.change(screen.getByRole("combobox"), { target: { value: query } });
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("option", { name: /en las fuentes/ }));
    expect(screen.getByLabelText("Destino")).toHaveTextContent(`/search?q=${encodeURIComponent(query)}`);
  });
  it("uses the actual chapter and does not offer completed chapters as quick resume", async () => {
    const view = setup();
    expect(await screen.findByRole("link", { name: "Continuar leyendo Obra reciente" })).toHaveAttribute("href", `/reader/${chapterId}`);
    view.unmount(); vi.mocked(api.getRecentReaderProgress).mockResolvedValue({ data: [{ ...progress, percentage: "100.00" }], meta: { limit: 4 } });
    setup(); await waitFor(() => expect(api.getRecentReaderProgress).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("link", { name: /Continuar leyendo/ })).not.toBeInTheDocument();
  });
  it("changes the existing theme without changing the other appearance preferences", () => {
    setup(); fireEvent.click(screen.getByRole("button", { name: "Abrir paleta de comandos" }));
    fireEvent.click(screen.getByRole("option", { name: /Cambiar al tema Papel/ }));
    expect(useAppearanceStore.getState()).toMatchObject({ theme: "papel", intensity: 35, enabled: true });
  });
  it("does not display a late result for an older search", async () => {
    let resolve!: (page: api.LibraryPage) => void;
    vi.mocked(api.getLibraryPage).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    setup(); fireEvent.click(screen.getByRole("button", { name: "Abrir paleta de comandos" }));
    const input = screen.getByRole("combobox"); fireEvent.change(input, { target: { value: "Anterior" } });
    await waitFor(() => expect(api.getLibraryPage).toHaveBeenCalledTimes(1));
    fireEvent.change(input, { target: { value: "Nueva" } });
    await act(async () => resolve({ data: [{ seriesId, displayTitle: "Resultado anterior" } as api.LibrarySeriesSummary], meta: { limit: 6, offset: 0, total: 1 } }));
    expect(screen.queryByRole("option", { name: /Resultado anterior/ })).not.toBeInTheDocument();
  });
});
