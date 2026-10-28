import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { HomePage } from "./HomePage";
import { useAppearanceStore } from "../app/appearanceStore";

vi.mock("../api/client", async (original) => ({ ...await original<typeof import("../api/client")>(),
  getRecentLibrary: vi.fn(), getRecentReaderProgress: vi.fn(), getSystemReadiness: vi.fn(), getRecommendations:vi.fn() }));

const alphaId = "123e4567-e89b-42d3-a456-426614174001";
const betaId = "123e4567-e89b-42d3-a456-426614174002";
const chapterId = "123e4567-e89b-42d3-a456-426614174003";
const work = (seriesId: string, displayTitle: string, genres: string[]): api.LibrarySeriesSummary => ({
  seriesId, displayTitle, genres, canonicalTitle: displayTitle, author: "Autor", description: "Sinopsis guardada",
  status: "ongoing", language: "es", coverAssetId: null, createdAt: "2026-10-21T10:00:00Z", updatedAt: "2026-10-21T10:00:00Z",
});
const library = { data: [work(alphaId, "Alfa", ["Acción"]), work(betaId, "Beta", ["Misterio"])], meta: {limit: 4} };
const progress = { data: [{seriesId: alphaId, seriesDisplayTitle: "Alfa", chapterId, chapterDisplayLabel: "Capítulo 2",
  pageIndex: 3, percentage: "30.00", updatedAt: "2026-10-21T10:00:00Z"}], meta: {limit: 4} };
function openHome(client = new QueryClient({defaultOptions: {queries: {retry: false, gcTime: 0}}})) {
  return render(<QueryClientProvider client={client}><MemoryRouter><HomePage /></MemoryRouter></QueryClientProvider>);
}
beforeEach(() => {
  useAppearanceStore.setState({simpleGraphics: false});
  vi.stubGlobal("matchMedia", vi.fn(() => ({matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn()})));
  vi.mocked(api.getSystemReadiness).mockResolvedValue({status: "ready"});
  vi.mocked(api.getRecentLibrary).mockReset().mockResolvedValue(library);
  vi.mocked(api.getRecentReaderProgress).mockReset().mockResolvedValue(progress);
  vi.mocked(api.getRecommendations).mockReset().mockResolvedValue({data:[{
    remoteId:betaId,title:"Beta",aliases:[],genres:["Misterio"],coverUrl:null,
  }],meta:{provider:"MangaDex",status:"ready",updatedAt:"2026-10-21T12:00:00Z",retryAt:null}});
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); useAppearanceStore.setState({simpleGraphics: false}); });

describe("featured reading", () => {
  it.each(["no history", "only finished chapters"])("hides the resume action with %s", async (kind) => {
    vi.mocked(api.getRecentReaderProgress).mockResolvedValue({
      ...progress, data: kind === "no history" ? [] : progress.data.map((item) => ({...item, percentage: "100.00"})),
    });
    openHome();
    await screen.findByRole("heading", {level: 2, name: "Alfa"});
    expect(screen.queryByRole("link", {name: "Continuar leyendo"})).not.toBeInTheDocument();
    expect(screen.getByRole("link", {name: "Mi biblioteca"})).toHaveAttribute("href", "/library");
  });

  it("rotates every eight seconds, pauses on interaction and lets the reader choose an exact slide", async () => {
    const view = openHome();
    await screen.findByRole("heading", {level: 2, name: "Alfa"});
    fireEvent.click(screen.getByRole("button", {name: "Pausar destacados"}));
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", {name: "Reanudar destacados"}));
    act(() => vi.advanceTimersByTime(7999));
    expect(screen.getByRole("heading", {level: 2, name: "Alfa"})).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByRole("heading", {level: 2, name: "Beta"})).toBeInTheDocument();
    const hero = screen.getByRole("region", {name: "Obras destacadas"});
    fireEvent.mouseEnter(hero);
    act(() => vi.advanceTimersByTime(24000));
    expect(screen.getByRole("heading", {level: 2, name: "Beta"})).toBeInTheDocument();
    fireEvent.mouseLeave(hero);
    fireEvent.focus(screen.getByRole("button", {name: "Destacado siguiente"}));
    act(() => vi.advanceTimersByTime(16000));
    expect(screen.getByRole("heading", {level: 2, name: "Beta"})).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Ver destacado: Alfa"}));
    expect(screen.getByRole("link", {name: "Continuar lectura: Capítulo 2"})).toHaveAttribute("href", `/reader/${chapterId}`);
    expect(screen.getByRole("link", {name: "Elegir capítulo de Alfa"})).toHaveAttribute("href", `/series/${alphaId}#chapters`);
    view.unmount();
    expect(() => act(() => vi.advanceTimersByTime(16000))).not.toThrow();
  });

  it.each(["reduced motion", "simple graphics"])("keeps manual navigation without automatic rotation with %s", async (preference) => {
    if (preference === "reduced motion") vi.stubGlobal("matchMedia", vi.fn(() => ({matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn()})));
    else useAppearanceStore.setState({simpleGraphics: true});
    openHome();
    await screen.findByRole("heading", {level: 2, name: "Alfa"});
    vi.useFakeTimers();
    act(() => vi.advanceTimersByTime(40000));
    expect(screen.getByRole("heading", {level: 2, name: "Alfa"})).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Destacado siguiente"}));
    expect(screen.getByRole("heading", {level: 2, name: "Beta"})).toBeInTheDocument();
  });

  it("keeps cached library and the exact reading position visible while the local service recovers", async () => {
    const client = new QueryClient({defaultOptions: {queries: {retry: false, gcTime: 0}}});
    client.setQueryData(["library", "recent", 4], library);
    client.setQueryData(["reader", "progress", "recent", 4, "series"], progress);
    vi.mocked(api.getSystemReadiness).mockResolvedValue({status: "not_ready"});
    openHome(client);
    await screen.findByText("El servicio local no está listo.");
    expect(screen.getByRole("link", {name: "Continuar lectura: Capítulo 2"})).toHaveAttribute("href", `/reader/${chapterId}`);
    expect(screen.getByText("Capítulo 2 · Página 4 · 30.00 %")).toBeInTheDocument();
    expect(api.getRecentLibrary).not.toHaveBeenCalled();
    expect(api.getRecentReaderProgress).not.toHaveBeenCalled();
  });

  it("uses stored genres for suggestions and never presents sample rankings or new chapters", async () => {
    openHome();
    await screen.findByRole("heading", {level: 2, name: "Alfa"});
    expect(screen.getByRole("region",{name:"Populares online"})).toBeInTheDocument();
    expect(screen.queryByText("No hay novedades verificadas de capítulos")).not.toBeInTheDocument();
    const discoveries = screen.getByRole("region", {name: "Descubre algo nuevo"});
    expect(screen.getAllByRole("heading", {level: 1})).toHaveLength(1);
    expect(screen.getByRole("heading", {level: 1})).toHaveTextContent("Descubre tu próxima historia");
    expect(discoveries.compareDocumentPosition(screen.getByRole("region", {name: "Continuar leyendo"})) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("link", {name: "Continuar leyendo"})).toHaveAttribute("href", "#recent-progress-title");
    fireEvent.click(within(discoveries).getByRole("button", {name: "Misterio"}));
    expect(within(discoveries).getByText("Popular en MangaDex")).toBeInTheDocument();
    expect(within(discoveries).getByRole("link", {name: "Beta"})).toHaveAttribute("href", "/search?q=Beta");
    expect(screen.queryByText("Porque lees Misterio")).not.toBeInTheDocument();
    expect(screen.queryByText(/Fuente A|Cap\. 24 nuevo|En 3 fuentes/)).not.toBeInTheDocument();
  });
});
