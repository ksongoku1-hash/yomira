import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConnectorDescriptor } from "../api/client";
import { SearchPage } from "./SearchPage";

function mockResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => payload,
  } as unknown as Response;
}

const defaultConnectors: ConnectorDescriptor[] = [
  {
    connectorId: "yomira.mock",
    connectorVersion: "1.0.0",
    capabilities: ["SEARCH", "CHAPTER_LIST", "PAGE_LIST"],
  },
];

function renderSearchPage({
  connectors = defaultConnectors,
  loadCatalog = false,
  initialEntries = ["/search"],
}: {
  connectors?: ConnectorDescriptor[];
  loadCatalog?: boolean;
  initialEntries?: string[];
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  if (!loadCatalog) queryClient.setQueryData(["connectors"], { data: connectors });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={initialEntries}>
          <SearchPage />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SearchPage", () => {
  it("selects only missing chapters in numeric order for the linked edition", async () => {
    let holdAvailability = false;
    let resolveAvailability: ((response: Response) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/availability")) return holdAvailability
        ? new Promise<Response>((resolve) => { resolveAvailability = resolve; })
        : Promise.resolve(mockResponse({ data: {
        linked: true, ready_remote_ids: ["chapter-1"], ready_labels: [],
        busy_remote_ids: ["chapter-2"],
      } }));
      if (path.endsWith("/chapters")) return Promise.resolve(mockResponse({
        data: [10, 2, 1, 3].map((number) => ({
          remote_id: `chapter-${number}`, display_label: `Capítulo ${number}`,
          remote_label: String(number), chapter_raw_label: String(number),
          chapter_sort_key: [number], language: null,
        })),
        meta: { connector_id: "yomira.mock", remote_series_id: "fixture/series" },
      }));
      return Promise.resolve(mockResponse({
        data: [{ remote_id: "fixture/series", remote_url: null, remote_title: "Fixture" }],
        meta: { connector_id: "yomira.mock", query: "fixture" },
      }));
    }));
    const {queryClient} = renderSearchPage({ initialEntries: ["/search?q=fixture"] });
    fireEvent.click(await screen.findByRole("button", { name: /Ver capítulos de Fixture/ }));
    await screen.findByRole("checkbox", { name: "Capítulo 1" });
    const pending = await screen.findByRole("button", { name: /Seleccionar pendientes/ });
    await waitFor(() => expect(pending).toBeEnabled());
    fireEvent.click(pending);
    const choices = screen.getAllByRole("checkbox");
    expect(choices[0]).toBeDisabled();
    expect(choices[0]).not.toBeChecked();
    expect(choices[1]).toBeDisabled();
    expect(choices[1]).not.toBeChecked();
    expect(choices[2]).toBeChecked();
    expect(choices[3]).toBeChecked();
    expect(choices.map((element) => element.parentElement?.textContent))
      .toEqual(["Capítulo 1Ya descargado", "Capítulo 2En cola o en curso",
        "Capítulo 3Sin descargar", "Capítulo 10Sin descargar"]);
    expect(screen.getByRole("button", { name: "Descargar (2)" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Seleccionar visibles" }));
    expect(screen.getByRole("button", { name: "Descargar (2)" })).toBeEnabled();
    expect(screen.queryByRole("option", { name: "Capítulo 1" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Capítulo 2" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Cambiar orden de capítulos para descargar"}));
    expect(screen.getAllByRole("checkbox").map((element) => element.parentElement?.textContent))
      .toEqual(["Capítulo 10Sin descargar", "Capítulo 3Sin descargar", "Capítulo 2En cola o en curso", "Capítulo 1Ya descargado"]);
    expect(screen.getByRole("checkbox", {name: "Capítulo 3"})).toBeChecked();
    fireEvent.change(screen.getByRole("combobox", {name: "Estado del capítulo"}), {target: {value: "ready"}});
    expect(screen.getAllByRole("checkbox")).toHaveLength(1);
    expect(screen.getByRole("checkbox", {name: "Capítulo 1"})).toBeDisabled();
    expect(screen.getByRole("button", {name: /Seleccionar pendientes/})).toBeDisabled();
    expect(screen.getByRole("button", {name: "Descargar (2)"})).toBeEnabled();
    fireEvent.change(screen.getByRole("combobox", {name: "Estado del capítulo"}), {target: {value: "missing"}});
    expect(screen.getAllByRole("checkbox")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", {name: "Invertir selección visible"}));
    expect(screen.getByRole("button", {name: "Descargar (0)"})).toBeDisabled();
    holdAvailability = true;
    const checking = queryClient.invalidateQueries({queryKey: ["chapter-availability"]});
    await waitFor(() => expect(resolveAvailability).toBeDefined());
    const checkButton = screen.getByRole("button", {name: "Comprobar descargados y faltantes"});
    expect(checkButton).toBeEnabled();
    expect(screen.getByRole("checkbox", {name: "Capítulo 3"})).toBeEnabled();
    resolveAvailability!(mockResponse({data: {
      linked: true, ready_remote_ids: ["chapter-1", "chapter-3"], ready_labels: [], busy_remote_ids: ["chapter-2"],
    }}));
    await checking;
    await waitFor(() => expect(screen.queryByRole("checkbox", {name: "Capítulo 3"})).not.toBeInTheDocument());
    fireEvent.click(checkButton);
    expect(await screen.findByRole("button", {name: "Comprobando capítulos…"})).toBeDisabled();
    resolveAvailability!(mockResponse({data: {linked: true, ready_remote_ids: [], ready_labels: [], busy_remote_ids: []}}));
    await waitFor(() => expect(checkButton).toBeEnabled());
  });
  it.each([
    ["Capítulo 001", ["Capítulo: 1"], true, true],
    ["Capítulo 2.50", ["2,5"], true, true],
    ["Capítulo 2.50", ["2.05"], true, false],
    ["Capítulo 1", ["Chapter 1", "Capítulo 01"], true, false],
    ["Capítulo 1", ["Chapter 1"], false, false],
  ])("compares imported labels safely: %s and %s", async (localLabel, remoteLabels, linked, disabled) => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/availability")) return Promise.resolve(mockResponse({ data: {
        linked, ready_remote_ids: [], ready_labels: [localLabel], busy_remote_ids: [],
      } }));
      if (path.endsWith("/chapters")) return Promise.resolve(mockResponse({
        data: remoteLabels.map((label, index) => ({
          remote_id: `chapter-${index}`, display_label: label, remote_label: label,
          chapter_raw_label: label, chapter_sort_key: [index], language: null,
        })), meta: { connector_id: "yomira.mock", remote_series_id: "fixture" },
      }));
      return Promise.resolve(mockResponse({ data: [{ remote_id: "fixture", remote_url: null,
        remote_title: "Fixture" }], meta: { connector_id: "yomira.mock", query: "fixture" } }));
    }));
    renderSearchPage({ initialEntries: ["/search?q=fixture"] });
    fireEvent.click(await screen.findByRole("button", { name: /Ver capítulos de Fixture/ }));
    await screen.findByText(/disponibles para leer/);
    for (const choice of screen.getAllByRole("checkbox")) {
      if (disabled) expect(choice).toBeDisabled();
      else expect(choice).toBeEnabled();
    }
  });

  it("waits for library comparison and permits retry after a comparison error", async () => {
    let fail = true;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/availability")) return Promise.resolve(fail ? mockResponse({ error: {
        code: "API_VALIDATION_FAILED", message: "Error de prueba", retryable: true, trace_id: "comparison",
      } }, 503) : mockResponse({ data: {
        linked: true, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
      } }));
      if (path.endsWith("/chapters")) return Promise.resolve(mockResponse({ data: [{
        remote_id: "chapter-1", display_label: "Capítulo 1", remote_label: "1",
        chapter_raw_label: "1", chapter_sort_key: [1], language: null,
      }], meta: { connector_id: "yomira.mock", remote_series_id: "fixture" } }));
      return Promise.resolve(mockResponse({ data: [{ remote_id: "fixture", remote_url: null,
        remote_title: "Fixture" }], meta: { connector_id: "yomira.mock", query: "fixture" } }));
    }));
    const { queryClient } = renderSearchPage({ initialEntries: ["/search?q=fixture"] });
    fireEvent.click(await screen.findByRole("button", { name: /Ver capítulos de Fixture/ }));
    await screen.findByText(/No se pudo comparar/);
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Descargar (0)" })).toBeDisabled();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Comprobar descargados y faltantes" }));
    await waitFor(() => expect(screen.getByRole("checkbox")).toBeEnabled());
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "Descargar (1)" })).toBeEnabled();
    queryClient.setQueryData(["chapter-availability", "yomira.mock", "fixture"], {
      linked: true, readyRemoteIds: [], readyLabels: [], busyRemoteIds: ["chapter-1"],
    });
    await waitFor(() => expect(screen.getByRole("checkbox")).toBeDisabled());
    expect(screen.getByRole("button", { name: "Descargar (0)" })).toBeDisabled();
  });
  it("selects separate chapters, preserves hidden selections, and bounds bulk and range selection", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/availability")) return Promise.resolve(mockResponse({ data: {
        linked: false, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
      } }));
      if (url.endsWith("/download-jobs")) return Promise.resolve(mockResponse({
        error: { code: "API_VALIDATION_FAILED", message: "Prueba de reintento", retryable: true, trace_id: "retry" },
      }, 422));
      if (url.endsWith("/chapters")) return Promise.resolve(mockResponse({
        data: [...Array.from({ length: 105 }, (_, i) => ({
          remote_id: `chapter-${i + 1}`, display_label: `Capítulo ${i + 1}`,
          remote_label: String(i + 1), chapter_raw_label: String(i + 1),
          chapter_sort_key: [i + 1], language: null,
        })), {
          remote_id: null, display_label: "Especial sin referencia",
          remote_label: "Extra", chapter_raw_label: "Extra", chapter_sort_key: [106], language: null,
        }],
        meta: { connector_id: "yomira.mock", remote_series_id: "fixture/series" },
      }));
      return Promise.resolve(mockResponse({
        data: [{ remote_id: "fixture/series", remote_url: null, remote_title: "Fixture" }],
        meta: { connector_id: "yomira.mock", query: "fixture" },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage({ initialEntries: ["/search?q=fixture"] });
    fireEvent.click(await screen.findByRole("button", { name: /Ver capítulos de Fixture/ }));
    const first = await screen.findByRole("checkbox", { name: "Capítulo 1" });
    fireEvent.click(first);
    fireEvent.click(screen.getByRole("checkbox", { name: "Capítulo 3" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Filtrar capítulos" }), { target: { value: "105" } });
    expect(screen.queryByRole("checkbox", { name: "Capítulo 1" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Descargar (2)" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Descargar (2)" }));
    await screen.findByRole("alert");
    expect(fetchMock).toHaveBeenLastCalledWith(expect.stringContaining("/download-jobs"), expect.objectContaining({
      body: JSON.stringify({ chapter_remote_ids: ["chapter-1", "chapter-3"], priority: "NORMAL" }),
    }));
    expect(screen.getByRole("button", { name: "Descargar (2)" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Seleccionar visibles" }));
    expect(screen.getByRole("checkbox", { name: "Capítulo 105" })).toBeChecked();
    fireEvent.change(screen.getByRole("searchbox", { name: "Filtrar capítulos" }), { target: { value: "" } });
    expect(screen.getByRole("checkbox", { name: "Especial sin referencia" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Seleccionar primeros 100" }));
    expect(screen.getByRole("button", { name: "Descargar (100)" })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "Capítulo 101" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: "Capítulo 1" }));
    expect(screen.getByRole("checkbox", { name: "Capítulo 101" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Limpiar selección" }));
    expect(screen.getByRole("button", { name: "Descargar (0)" })).toBeDisabled();
    fireEvent.change(screen.getByRole("combobox", { name: "Desde" }), { target: { value: "chapter-3" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Hasta" }), { target: { value: "chapter-6" } });
    expect(screen.getByRole("button", { name: "Descargar (4)" })).toBeEnabled();
    for (const id of [3, 4, 5, 6]) expect(screen.getByRole("checkbox", { name: `Capítulo ${id}` })).toBeChecked();
  }, 15_000);

  it("shows the Webtoon source label returned by FMD2", async () => {
    const connectorId = "yomira.fmd2.webtoons";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse({
      data: [{
        remote_id: "fmd2:v1:series",
        remote_url: null,
        remote_title: "Mato Seihei",
        cover_url: "https://swebtoon-phinf.pstatic.net/cover.webp",
        source_label: "WEBTOON (en)",
      }],
      meta: { connector_id: connectorId, query: "Mato Seihei" },
    })));
    renderSearchPage({
      connectors: [{
        connectorId,
        connectorVersion: "1.0.0",
        capabilities: ["SEARCH", "METADATA", "CHAPTER_LIST", "PAGE_LIST"],
      }],
      initialEntries: ["/search?q=Mato+Seihei"],
    });

    expect(await screen.findByRole("heading", { level: 2, name: "Mato Seihei" }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: "Ver capítulos de Mato Seihei en WEBTOON (en)",
    })).toBeInTheDocument();
  });

  it("filters source choices without running the search again", async () => {
    const connectorId = "yomira.fmd2.webtoons";
    const fetchMock = vi.fn().mockResolvedValue(mockResponse({
      data: ["InManga", "NovelCoolES"].map((source) => ({
        remote_id: source,
        remote_url: null,
        remote_title: "Naruto",
        source_label: source,
        cover_url: "https://source.example/naruto.png",
      })),
      meta: { connector_id: connectorId, query: "Naruto" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage({
      connectors: [{
        connectorId, connectorVersion: "1.0.0",
        capabilities: ["SEARCH", "METADATA", "CHAPTER_LIST", "PAGE_LIST"],
      }],
      initialEntries: ["/search?q=Naruto"],
    });
    const filter = await screen.findByRole("combobox", { name: "Mostrar resultados de" });
    expect(screen.getByRole("button", { name: "Ver capítulos de Naruto en NovelCoolES" })).toBeInTheDocument();
    fireEvent.change(filter, { target: { value: "InManga" } });
    expect(screen.getByRole("button", { name: "Ver capítulos de Naruto en InManga" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ver capítulos de Naruto en NovelCoolES" })).not.toBeInTheDocument();
    fireEvent.change(filter, { target: { value: "" } });
    expect(screen.getByRole("button", { name: "Ver capítulos de Naruto en NovelCoolES" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("labels the download connector as Free Manga", async () => {
    const connectorId = "yomira.fmd2.webtoons";
    const fetchMock = vi.fn().mockResolvedValue(mockResponse({
      data: [{
        remote_id: "fmd2:v1:series",
        remote_url: null,
        remote_title: "Public Series",
        cover_url: null,
      }],
      meta: { connector_id: connectorId, query: "Public" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage({
      connectors: [{
        connectorId,
        connectorVersion: "1.0.0",
        capabilities: ["SEARCH", "METADATA", "CHAPTER_LIST", "PAGE_LIST"],
      }],
      initialEntries: ["/search?q=Public"],
    });

    expect(await screen.findByRole("heading", { level: 2, name: "Public Series" }))
      .toBeInTheDocument();
    expect(screen.getByText("Free Manga")).toBeInTheDocument();
  });

  it("loads a missing cover from the source metadata on demand", async () => {
    const connectorId = "yomira.fmd2.webtoons";
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/availability")) return Promise.resolve(mockResponse({ data: {
        linked: false, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
      } }));
      if (url.includes("/search?")) {
        return Promise.resolve(mockResponse({
          data: [{
            remote_id: "fmd2:v1:dragon-ball",
            remote_url: "https://zonatmo.org/library/manga/dragon-ball",
            remote_title: "Dragon Ball",
            cover_url: null,
          }],
          meta: { connector_id: connectorId, query: "dragon ball" },
        }));
      }
      if (url.includes("/cover?title=")) {
        return Promise.resolve(mockResponse({
          data: { cover_url: "https://zonatmo.org/storage/covers/dragon-ball.jpg" },
          meta: { connector_id: connectorId, remote_series_id: "fmd2:v1:dragon-ball" },
        }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("IntersectionObserver", undefined);
    renderSearchPage({
      connectors: [{
        connectorId,
        connectorVersion: "1.0.0",
        capabilities: ["SEARCH", "METADATA"],
      }],
      initialEntries: ["/search?q=dragon+ball"],
    });

    expect(await screen.findByRole("img", { name: "Portada de Dragon Ball" }))
      .toHaveAttribute("src", "https://zonatmo.org/storage/covers/dragon-ball.jpg");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.fmd2.webtoons/series/fmd2%3Av1%3Adragon-ball/cover?title=Dragon%20Ball",
      { headers: { Accept: "application/json" } },
    );
  });

  it("runs the prefilled search passed from a series detail page", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/availability")) return Promise.resolve(mockResponse({ data: {
        linked: false, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
      } }));
      const connectorId = url.includes("/yomira.fmd2.webtoons/")
        ? "yomira.fmd2.webtoons"
        : "yomira.mock";
      return Promise.resolve(mockResponse({
        data: connectorId === "yomira.fmd2.webtoons" ? [{
          remote_id: "mock-series-1",
          remote_url: "https://www.webtoons.com/en/mock-series-1",
          remote_title: "Mato Seihei no Slave",
          cover_url: "https://swebtoon-phinf.pstatic.net/mato.webp",
        }] : [],
        meta: { connector_id: connectorId, query: "Mato Seihei no Slave" },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage({
      connectors: [
        ...defaultConnectors,
        {
          connectorId: "yomira.fmd2.webtoons",
          connectorVersion: "1.0.0",
          capabilities: ["SEARCH", "METADATA", "CHAPTER_LIST"],
        },
      ],
      initialEntries: ["/search?q=Mato+Seihei+no+Slave&connector=yomira.fmd2.webtoons"],
    });

    expect(await screen.findByRole("heading", { level: 2, name: "Mato Seihei no Slave" }))
      .toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Título o palabras clave" }))
      .toHaveValue("Mato Seihei no Slave");
    expect(screen.queryByRole("combobox", { name: "Fuente de contenido" })).not.toBeInTheDocument();
    expect(screen.getByText("Free Manga")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Portada de Mato Seihei no Slave" }))
      .toHaveAttribute("src", "https://swebtoon-phinf.pstatic.net/mato.webp");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.fmd2.webtoons/search?query=Mato+Seihei+no+Slave",
      { headers: { Accept: "application/json" }, signal: expect.any(AbortSignal) },
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("caps the visible result cards per source", async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockResponse({
      data: Array.from({ length: 101 }, (_, index) => ({
        remote_id: `series-${index}`,
        remote_url: null,
        remote_title: `Series ${index}`,
        cover_url: null,
      })),
      meta: { connector_id: "yomira.mock", query: "series" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "series" } });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));

    expect(await screen.findByText("Series 99")).toBeInTheDocument();
    expect(screen.queryByText("Series 100")).not.toBeInTheDocument();
    expect(screen.getByText("Se muestran como máximo 100 resultados por fuente."))
      .toBeInTheDocument();
  });

  it("groups the same title into one cover card and opens chapters from the chosen source", async () => {
    const fixtureId = "fixture.catalog";
    const fixtureRemoteId = "/manga/mato-seihei-no-slave";
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/availability")) return Promise.resolve(mockResponse({ data: {
        linked: false, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
      } }));
      if (url.includes("/search?")) {
        const fromFixture = url.includes(`/connectors/${fixtureId}/`);
        return Promise.resolve(mockResponse({
          data: [{
            remote_id: fromFixture ? fixtureRemoteId : "mock-mato-seihei",
            remote_url: null,
            remote_title: fromFixture ? "Mátō Seihei no Slave" : "Mato Seihei-no Slave",
            cover_url: fromFixture ? "https://source.example/covers/mato.webp" : null,
          }],
          meta: { connector_id: fromFixture ? fixtureId : "yomira.mock", query: "Mato Seihei" },
        }));
      }
      if (url.includes("/chapters")) {
        return Promise.resolve(mockResponse({
          data: [{
            remote_id: `${fixtureRemoteId}/chapter-1`,
            display_label: "Capítulo 1",
            remote_label: "1",
            chapter_raw_label: "1",
            chapter_sort_key: [1],
            language: "es",
          }],
          meta: { connector_id: fixtureId, remote_series_id: fixtureRemoteId },
        }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage({
      connectors: [
        ...defaultConnectors,
        {
          connectorId: fixtureId,
          connectorVersion: "0.1.0",
          capabilities: ["SEARCH", "CHAPTER_LIST"],
        },
      ],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: "Título o palabras clave" }), {
      target: { value: "Mato Seihei" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));

    expect(await screen.findByRole("heading", { level: 2, name: "Mato Seihei-no Slave" }))
      .toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 2, name: /Mato Seihei/i })).toHaveLength(1);
    expect(screen.getByRole("button", {
      name: /Ver capítulos de Mato Seihei-no Slave en fixture.catalog/,
    })).toBeInTheDocument();
    expect(screen.queryByText("Capítulos")).not.toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: /Ver capítulos de Mato Seihei-no Slave en yomira.mock · prueba local/,
    })).toBeInTheDocument();
    expect(screen.queryByText("Descarga")).not.toBeInTheDocument();
    expect(screen.queryByText("Sin descarga")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Portada de Mato Seihei-no Slave" }))
      .toHaveAttribute("src", "https://source.example/covers/mato.webp");

    fireEvent.click(await screen.findByRole("button", {
      name: /Ver capítulos de Mato Seihei-no Slave en fixture.catalog/,
    }));
    expect(await screen.findByText("Capítulo 1")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/connectors/${fixtureId}/series/${encodeURIComponent(fixtureRemoteId)}/chapters`,
      { headers: { Accept: "application/json" } },
    );
  });

  it("stays idle until the user submits a non-empty query", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage();

    expect(screen.getByRole("heading", { level: 1, name: "Buscar" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Escribe un título para buscar en todas las fuentes.");
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Escribe un título para buscar.");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("loads the registered connector catalog and supports an explicit retry", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        mockResponse(
          {
            error: {
              code: "SYS_CATALOG_UNAVAILABLE",
              message: "No se pudo leer el catálogo local.",
              retryable: true,
              trace_id: "trace-connector-catalog",
            },
          },
          503,
        ),
      )
      .mockResolvedValueOnce(
        mockResponse({
          data: [{
            connector_id: "yomira.mock",
            connector_version: "1.0.0",
            capabilities: ["SEARCH", "CHAPTER_LIST"],
          }],
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage({ loadCatalog: true });

    expect(screen.getByRole("status")).toHaveTextContent("Cargando fuentes registradas");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("SYS_CATALOG_UNAVAILABLE");
    expect(alert).toHaveTextContent("trace-connector-catalog");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Reintentar lista de fuentes" }));
    expect(screen.queryByRole("combobox", { name: "Fuente de contenido" })).not.toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent("Escribe un título para buscar");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("uses registered search sources without showing a source selector", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage({
      connectors: [
        {
          connectorId: "custom.source",
          connectorVersion: "3.0.0",
          capabilities: ["SEARCH", "CHAPTER_LIST"],
        },
        {
          connectorId: "metadata.only",
          connectorVersion: "1.0.0",
          capabilities: ["METADATA"],
        },
      ],
    });

    expect(screen.queryByRole("combobox", { name: "Fuente de contenido" })).not.toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Título o palabras clave" })).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("todas las fuentes");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not enable search when the authoritative catalog is empty", () => {
    renderSearchPage({ connectors: [] });

    expect(screen.getByText("No hay conectores registrados en el catálogo local."))
      .toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Título o palabras clave" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Buscar" })).toBeDisabled();
  });

  it("adds a catalog result to the library and lists chapters without download controls", async () => {
    const connectorId = "fixture.catalog";
    const remoteSeriesId = "/manga/fate-zero";
    const seriesId = "123e4567-e89b-42d3-a456-426614174050";
    const meta = { connector_id: connectorId, remote_series_id: remoteSeriesId };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/availability")) return Promise.resolve(mockResponse({ data: {
        linked: false, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
      } }));
      if (url.endsWith("/library-series")) {
        return Promise.resolve(mockResponse({
          data: { series_id: seriesId, series_title: "Fate Zero", created: true }, meta,
        }));
      }
      if (url.endsWith("/chapters")) {
        return Promise.resolve(mockResponse({
          data: [{
            remote_id: "/manga/fate-zero/leer/1", display_label: "Capítulo 1", remote_label: "1",
            chapter_raw_label: "1", chapter_sort_key: [1], language: "es",
          }], meta,
        }));
      }
      return Promise.resolve(mockResponse({
        data: [{
          remote_id: remoteSeriesId,
          remote_url: null,
          remote_title: "Fate Zero",
          cover_url: "https://source.example/fate-zero.jpg",
        }],
        meta: { connector_id: connectorId, query: "Fate" },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { queryClient } = renderSearchPage({ connectors: [{
      connectorId, connectorVersion: "0.1.0", capabilities: ["SEARCH", "METADATA", "CHAPTER_LIST"],
    }] });
    queryClient.setQueryData(["library", "all", 0], { fixture: true });
    queryClient.setQueryData(["library", "recent", 4], { fixture: true });
    queryClient.setQueryData(["series", seriesId], { fixture: true });
    expect(screen.queryByRole("combobox", { name: "Fuente de contenido" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "Fate" } });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
    fireEvent.click(await screen.findByRole("button", { name: "Ver capítulos de Fate Zero en fixture.catalog" }));
    expect(await screen.findByText("Capítulo 1")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Prioridad" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Crear trabajos|Seleccionar todos/ }))
      .not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Añadir a biblioteca" }));
    expect(await screen.findByText("Se añadió «Fate Zero» a la biblioteca.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "En biblioteca" })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/fixture.catalog/series/%2Fmanga%2Ffate-zero/library-series",
      { method: "POST", headers: { Accept: "application/json" } },
    );
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(queryClient.getQueryState(["library", "all", 0])?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(["library", "recent", 4])?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(["series", seriesId])?.isInvalidated).toBe(true);
  });

  it("supports library import for metadata results without requesting chapters", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input).endsWith("/library-series")) {
        return Promise.resolve(mockResponse({
          data: {
            series_id: "123e4567-e89b-42d3-a456-426614174050",
            series_title: "Título conservado", created: false,
          },
          meta: { connector_id: "fixture.metadata", remote_series_id: "series-1" },
        }));
      }
      return Promise.resolve(mockResponse({
        data: [{
          remote_id: "series-1",
          remote_url: null,
          remote_title: "Fate Zero",
          cover_url: "https://source.example/fate-zero.jpg",
        }],
        meta: { connector_id: "fixture.metadata", query: "Fate" },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage({ connectors: [{
      connectorId: "fixture.metadata", connectorVersion: "1.0.0", capabilities: ["SEARCH", "METADATA"],
    }] });
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "Fate" } });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
    fireEvent.click(await screen.findByRole("button", { name: /Ver ficha de Fate Zero/ }));
    expect(screen.getByText("Fuente de metadatos; descarga no disponible desde esta fuente"))
      .toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Añadir a biblioteca" }));
    expect(await screen.findByText("«Título conservado» ya está en la biblioteca."))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /capítulos/i })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps an import failure trace and refreshes the library only after an explicit successful retry", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(mockResponse({
        data: [{
          remote_id: "/manga/fate-zero",
          remote_url: null,
          remote_title: "Fate Zero",
          cover_url: "https://source.example/fate-zero.jpg",
        }],
        meta: { connector_id: "fixture.catalog", query: "Fate" },
      }))
      .mockResolvedValueOnce(mockResponse({
        error: {
          code: "SRC_CONNECTOR_FAILED", message: "La fuente no está disponible.",
          retryable: false, trace_id: "trace-library-import",
        },
      }, 502))
      .mockResolvedValueOnce(mockResponse({
        data: {
          series_id: "123e4567-e89b-42d3-a456-426614174050",
          series_title: "Fate Zero", created: true,
        },
        meta: { connector_id: "fixture.catalog", remote_series_id: "/manga/fate-zero" },
      }));
    vi.stubGlobal("fetch", fetchMock);
    const { queryClient } = renderSearchPage({ connectors: [{
      connectorId: "fixture.catalog", connectorVersion: "0.1.0", capabilities: ["SEARCH", "METADATA"],
    }] });
    queryClient.setQueryData(["library", "all", 0], { fixture: true });
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "Fate" } });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
    fireEvent.click(await screen.findByRole("button", { name: /Ver ficha de Fate Zero en fixture.catalog/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Añadir a biblioteca" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("trace-library-import");
    expect(screen.queryByRole("button", { name: "En biblioteca" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(queryClient.getQueryState(["library", "all", 0])?.isInvalidated).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Reintentar alta en biblioteca" }));
    expect(await screen.findByText("Se añadió «Fate Zero» a la biblioteca.")).toBeInTheDocument();
    expect(queryClient.getQueryState(["library", "all", 0])?.isInvalidated).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("submits explicitly and expands one result to load its chapters", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/availability")) return Promise.resolve(mockResponse({ data: {
        linked: false, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
      } }));
      if (url.includes("/chapters")) {
        return Promise.resolve(
          mockResponse({
            data: [
              {
                remote_id: "chapter-1",
                display_label: "Capítulo 1",
                remote_label: "1",
                chapter_raw_label: "1",
                chapter_sort_key: [1, 0],
                language: "es",
              },
            ],
            meta: { connector_id: "yomira.mock", remote_series_id: "fixture/series" },
          }),
        );
      }
      return Promise.resolve(
        mockResponse({
          data: [
            {
              remote_id: "fixture/series",
              remote_url: "https://example.test/fixture/series",
              remote_title: "Mock Fixture Series",
            },
          ],
            meta: { connector_id: "yomira.mock", query: " fixture " },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage();

    fireEvent.change(screen.getByRole("searchbox", { name: "Título o palabras clave" }), {
      target: { value: " fixture " },
    });
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));

    expect(await screen.findByRole("heading", { level: 2, name: "Mock Fixture Series" }))
      .toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.mock/search?query=+fixture+",
      { headers: { Accept: "application/json" }, signal: expect.any(AbortSignal) },
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /Ver capítulos de Mock Fixture Series/ }));
    expect(await screen.findAllByRole("option", { name: "Capítulo 1" })).toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.mock/series/fixture%2Fseries/chapters",
      { headers: { Accept: "application/json" } },
    );
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("searches FMD2 and queues chapters from the source shown on its result", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/availability")) return Promise.resolve(mockResponse({ data: {
        linked: false, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
      } }));
      if (url.includes("/search?")) {
        const connectorId = url.includes("/connectors/yomira.fmd2.webtoons/")
          ? "yomira.fmd2.webtoons"
          : "yomira.mock";
        const title = connectorId === "yomira.fmd2.webtoons" ? "WEBTOON Series" : "Mock Series";
        return Promise.resolve(mockResponse({
          data: [{ remote_id: "series-1", remote_url: null, remote_title: title }],
          meta: { connector_id: connectorId, query: "fixture" },
        }));
      }
      if (url.includes("/chapters")) {
        const connectorId = url.includes("/connectors/yomira.fmd2.webtoons/")
          ? "yomira.fmd2.webtoons"
          : "yomira.mock";
        return Promise.resolve(mockResponse({
          data: [{
            remote_id: "chapter-1",
            display_label: "Capítulo 1",
            remote_label: "1",
            chapter_raw_label: "1",
            chapter_sort_key: [1],
            language: "es",
          }],
          meta: { connector_id: connectorId, remote_series_id: "series-1" },
        }));
      }
      if (url.endsWith("/download-jobs")) {
        return Promise.resolve(mockResponse({
          data: [{
            job_id: "123e4567-e89b-42d3-a456-426614174000",
            chapter_source_id: "123e4567-e89b-42d3-a456-426614174001",
            series_id: "123e4567-e89b-42d3-a456-426614174050",
            chapter_id: "123e4567-e89b-42d3-a456-426614174051",
            status: "QUEUED",
            priority: "NORMAL",
            series_title: "WEBTOON Series",
            chapter_label: "Capítulo 1",
            created: true,
            created_at: "2026-09-27T10:00:00Z",
            updated_at: "2026-09-27T10:00:00Z",
          }],
          meta: { connector_id: "yomira.fmd2.webtoons", remote_series_id: "series-1" },
        }));
      }
      throw new Error(`Unexpected connector request: ${url} ${String(init?.method ?? "GET")}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage({
      connectors: [
        ...defaultConnectors,
        {
          connectorId: "yomira.fmd2.webtoons",
          connectorVersion: "1.0.0",
          capabilities: ["SEARCH", "METADATA", "CHAPTER_LIST", "PAGE_LIST"],
        },
      ],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: "Título o palabras clave" }), {
      target: { value: "fixture" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Mock Series" }))
      .toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 2, name: "WEBTOON Series" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Fuente de contenido" })).not.toBeInTheDocument();
    expect(screen.getByText("Free Manga")).toBeInTheDocument();
    expect(screen.queryByText("WEBTOON (yomira.fmd2.webtoons)")).not.toBeInTheDocument();
    expect(screen.getByText("yomira.mock · prueba local")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.fmd2.webtoons/search?query=fixture",
      { headers: { Accept: "application/json" }, signal: expect.any(AbortSignal) },
    );

    fireEvent.click(screen.getByRole("button", { name: /Ver capítulos de WEBTOON Series/ }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Desde" }), {
      target: { value: "chapter-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Descargar (1)" }));
    expect(await screen.findByText(/Se añadió 1 capítulo a la cola/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.fmd2.webtoons/series/series-1/chapters",
      { headers: { Accept: "application/json" } },
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.fmd2.webtoons/series/series-1/download-jobs",
      {
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        method: "POST",
        body: JSON.stringify({ chapter_remote_ids: ["chapter-1"], priority: "NORMAL" }),
      },
    );
  });

  it("selects chapters and explains that saved jobs are processed automatically", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/availability")) return Promise.resolve(mockResponse({ data: {
        linked: false, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
      } }));
      if (url.endsWith("/download-jobs")) {
        expect(init?.method).toBe("POST");
        return Promise.resolve(
          mockResponse({
            data: [
              {
                job_id: "123e4567-e89b-42d3-a456-426614174000",
                chapter_source_id: "123e4567-e89b-42d3-a456-426614174001",
                series_id: "123e4567-e89b-42d3-a456-426614174050",
                chapter_id: "123e4567-e89b-42d3-a456-426614174051",
                status: "QUEUED",
                priority: "HIGH",
                series_title: "Mock Fixture Series",
                chapter_label: "Capítulo 1",
                created: true,
                created_at: "2026-09-27T10:00:00Z",
                updated_at: "2026-09-27T10:00:00Z",
              },
              {
                job_id: "123e4567-e89b-42d3-a456-426614174002",
                chapter_source_id: "123e4567-e89b-42d3-a456-426614174003",
                series_id: "123e4567-e89b-42d3-a456-426614174050",
                chapter_id: "123e4567-e89b-42d3-a456-426614174052",
                status: "QUEUED",
                priority: "HIGH",
                series_title: "Mock Fixture Series",
                chapter_label: "Capítulo 2",
                created: true,
                created_at: "2026-09-27T10:00:00Z",
                updated_at: "2026-09-27T10:00:00Z",
              },
            ],
            meta: { connector_id: "yomira.mock", remote_series_id: "fixture/series" },
          }),
        );
      }
      if (url.includes("/chapters")) {
        return Promise.resolve(
          mockResponse({
            data: [
              {
                remote_id: "chapter-1",
                display_label: "Capítulo 1",
                remote_label: "1",
                chapter_raw_label: "1",
                chapter_sort_key: [1],
                language: "es",
              },
              {
                remote_id: "chapter-2",
                display_label: "Capítulo 2",
                remote_label: "2",
                chapter_raw_label: "2",
                chapter_sort_key: [2],
                language: "es",
              },
            ],
            meta: { connector_id: "yomira.mock", remote_series_id: "fixture/series" },
          }),
        );
      }
      return Promise.resolve(
        mockResponse({
          data: [
            {
              remote_id: "fixture/series",
              remote_url: "https://example.test/fixture/series",
              remote_title: "Mock Fixture Series",
            },
          ],
          meta: { connector_id: "yomira.mock", query: "fixture" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage();

    fireEvent.change(screen.getByRole("searchbox", { name: "Título o palabras clave" }), {
      target: { value: "fixture" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
    fireEvent.click(await screen.findByRole("button", { name: /Ver capítulos de Mock Fixture Series/ }));

    const start = await screen.findByRole("combobox", { name: "Desde" });
    const end = screen.getByRole("combobox", { name: "Hasta" });
    expect(screen.getByText(/los trabajos se procesan en segundo plano/i))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Descargar (0)" })).toBeDisabled();
    fireEvent.change(start, { target: { value: "chapter-1" } });
    fireEvent.change(end, { target: { value: "chapter-2" } });
    expect(screen.getByText(/2\s+capítulos seleccionados/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Prioridad" }), {
      target: { value: "HIGH" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Descargar (2)" }));

    expect(await screen.findByText(/Se añadieron 2 capítulos a la cola/))
      .toHaveTextContent("La descarga comenzará automáticamente; consulta «Cola de descargas» para ver su estado.");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/connectors/yomira.mock/series/fixture%2Fseries/download-jobs",
      {
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        method: "POST",
        body: JSON.stringify({ chapter_remote_ids: ["chapter-1", "chapter-2"], priority: "HIGH" }),
      },
    );
  });

  it("keeps the selected chapters and trace details when job creation fails", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/availability")) return Promise.resolve(mockResponse({ data: {
        linked: false, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
      } }));
      if (url.endsWith("/download-jobs")) {
        return Promise.resolve(
          mockResponse(
            {
              error: {
                code: "API_VALIDATION_FAILED",
                message: "La selección ya no está disponible.",
                retryable: false,
                trace_id: "trace-create-jobs",
              },
            },
            422,
          ),
        );
      }
      if (url.includes("/chapters")) {
        return Promise.resolve(
          mockResponse({
            data: [
              {
                remote_id: "chapter-1",
                display_label: "Capítulo 1",
                remote_label: "1",
                chapter_raw_label: "1",
                chapter_sort_key: [1],
                language: "es",
              },
            ],
            meta: { connector_id: "yomira.mock", remote_series_id: "fixture/series" },
          }),
        );
      }
      return Promise.resolve(
        mockResponse({
          data: [{ remote_id: "fixture/series", remote_url: null, remote_title: "Fixture" }],
          meta: { connector_id: "yomira.mock", query: "fixture" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage();

    fireEvent.change(screen.getByRole("searchbox", { name: "Título o palabras clave" }), {
      target: { value: "fixture" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
    fireEvent.click(await screen.findByRole("button", { name: /Ver capítulos de Fixture/ }));
    const start = await screen.findByRole("combobox", { name: "Desde" });
    fireEvent.change(start, { target: { value: "chapter-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Descargar (1)" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("API_VALIDATION_FAILED");
    expect(alert).toHaveTextContent("trace-create-jobs");
    expect(start).toHaveValue("chapter-1");
    expect(screen.getByRole("combobox", { name: "Hasta" })).toHaveValue("chapter-1");
  });

  it("shows a distinct empty-result state after a successful search", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({ data: [], meta: { connector_id: "yomira.mock", query: "unknown" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage();

    fireEvent.change(screen.getByRole("searchbox", { name: "Título o palabras clave" }), {
      target: { value: "unknown" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "No se encontraron series para “unknown” en las fuentes consultadas.",
      ),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the API error code and trace ID and retries only when requested", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        mockResponse(
          {
            error: {
              code: "SRC_CONNECTOR_UNAVAILABLE",
              message: "El conector no está disponible.",
              retryable: true,
              trace_id: "trace-search-1",
            },
          },
          503,
        ),
      )
      .mockResolvedValueOnce(
        mockResponse({ data: [], meta: { connector_id: "yomira.mock", query: "fixture" } }),
      );
    vi.stubGlobal("fetch", fetchMock);
    renderSearchPage();

    fireEvent.change(screen.getByRole("searchbox", { name: "Título o palabras clave" }), {
      target: { value: "fixture" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("SRC_CONNECTOR_UNAVAILABLE");
    expect(alert).toHaveTextContent("trace-search-1");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Reintentar fuente" }));
    expect(await screen.findByRole("status")).toHaveTextContent("No se encontraron series");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });
});
