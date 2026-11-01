import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { useShellStore } from "./app/shellStore";

// The updater has its own integration tests; keep these request sequences scoped to each page.
vi.mock("./app/AppUpdates", () => ({ AppUpdates: () => null }));

function mockResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => payload,
  } as unknown as Response;
}

interface SeriesRecord {
  series_id: string;
  canonical_title: string;
  display_title: string;
  description: string | null;
  status: string | null;
  language: string | null;
  cover_asset_id: string | null;
  created_at: string;
  updated_at: string;
  metadata: MetadataRecord[];
}

interface MetadataRecord {
  field: string;
  value: string | string[] | null;
  authority: string | null;
  local_value: string | string[] | null;
  source_values: Array<{
    series_source_id: string;
    source_id: string;
    source_display_name: string | null;
    value: string | string[];
  }>;
  has_user_override: boolean;
  user_override_value: string | string[] | null;
  source_conflict: boolean;
  effective_source_ids: string[];
}

function series(
  id: string,
  displayTitle: string,
  coverAssetId: string | null = null,
): SeriesRecord {
  const metadata = (
    field: string,
    value: string | null,
    authority: string | null,
  ): MetadataRecord => ({
    field,
    value,
    authority,
    local_value: value,
    source_values: [],
    has_user_override: false,
    user_override_value: null,
    source_conflict: false,
    effective_source_ids: [],
  });

  return {
    series_id: id,
    canonical_title: displayTitle.toLowerCase(),
    display_title: displayTitle,
    description: null,
    status: null,
    language: "ja",
    cover_asset_id: coverAssetId,
    created_at: "2026-09-26T10:00:00Z",
    updated_at: "2026-09-26T10:00:00Z",
    metadata: [
      metadata("title", displayTitle, "LOCAL_CANONICAL"),
      metadata("cover", coverAssetId, coverAssetId ? "LOCAL_CANONICAL" : null),
      metadata("author", null, null),
      metadata("description", null, null),
      metadata("genres", null, null),
      metadata("status", null, null),
      metadata("language", "ja", "LOCAL_CANONICAL"),
    ],
  };
}

function syntheticSeriesId(index: number): string {
  return `123e4567-e89b-42d3-a456-${index.toString(16).padStart(12, "0")}`;
}

function libraryResponse(
  records: SeriesRecord[],
  total = records.length,
  offset = 0,
  limit = 100,
): Response {
  return mockResponse({ data: records, meta: { limit, offset, total } });
}

function recentLibraryResponse(records: SeriesRecord[], limit = 4): Response {
  return mockResponse({ data: records, meta: { limit } });
}

function recentReaderProgressResponse(
  records: Array<{
    series_id: string;
    display_title: string;
    chapter_id: string;
    display_label: string;
    page_index: number;
    percentage: string;
    updated_at: string;
  }>,
  limit = 4,
): Response {
  return mockResponse({ data: records, meta: { limit } });
}

const SYSTEM_DIAGNOSTIC_IDS = [
  "backend",
  "database",
  "library",
  "disk",
  "image_processor",
  "cbz_builder",
  "connectors",
  "lan",
] as const;

interface DiagnosticFixtureCheck {
  id: string;
  status: string;
  code: string | null;
  message: string;
  trace_id: string | null;
}

function systemDiagnosticsResponse(
  checks: DiagnosticFixtureCheck[],
  schemaVersion: string | null = "0019_usb_export_history",
): Response {
  const checksById = new Map(checks.map((check) => [check.id, check]));
  return mockResponse({
    app_version: "1.2.3",
    api_version: 1,
    schema_version: schemaVersion,
    checks: SYSTEM_DIAGNOSTIC_IDS.map((id) => checksById.get(id) ?? ({
      id,
      status: "unknown",
      code: null,
      message: "Comprobación no disponible.",
      trace_id: null,
    })),
  });
}

function renderLibrary() {
  window.history.replaceState({}, "", "/library");
  return render(<App />);
}

// Resolve route imports before tests that freeze the clock for polling.
beforeAll(async () => {
  await Promise.all([import("./library/LibraryPage"), import("./series/SeriesDetailPage"),
    import("./downloads/DownloadsPage"), import("./search/SearchPage")]);
});

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  localStorage.removeItem("yomira.sidebar");
  useShellStore.setState({ isNavigationOpen: false });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("remembers both sidebar positions after reopening the interface", () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async () => mockResponse({status: "not_ready"}, 503)));
    localStorage.setItem("yomira.sidebar", "expanded");
    const first = render(<App />);
    fireEvent.click(screen.getByRole("button", {name: "Contraer menú"}));
    expect(localStorage.getItem("yomira.sidebar")).toBe("collapsed");
    first.unmount();
    const second = render(<App />);
    fireEvent.click(screen.getByRole("button", {name: "Expandir menú"}));
    expect(localStorage.getItem("yomira.sidebar")).toBe("expanded");
    second.unmount();
    render(<App />);
    expect(screen.getByRole("button", {name: "Contraer menú"})).toHaveAttribute("aria-expanded", "true");
  });

  it.each(["invalid preference", "blocked storage"])("keeps the sidebar usable with %s", (kind) => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async () => mockResponse({status: "not_ready"}, 503)));
    if (kind === "invalid preference") localStorage.setItem("yomira.sidebar", "unexpected");
    else {
      vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
    }
    render(<App />);
    fireEvent.click(screen.getByRole("button", {name: "Expandir menú"}));
    expect(screen.getByRole("button", {name: "Contraer menú"})).toHaveAttribute("aria-expanded", "true");
  });

  it("shows the latest point per manga and sends a finished chapter to its chapter list", async () => {
    const finishedSeries = syntheticSeriesId(31);
    const ongoingSeries = syntheticSeriesId(32);
    const finishedChapter = syntheticSeriesId(41);
    const ongoingChapter = syntheticSeriesId(42);
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/system/readiness") return Promise.resolve(mockResponse({status: "ready"}));
      if (path.startsWith("/api/v1/reader/progress/recent?")) return Promise.resolve(recentReaderProgressResponse([
        {series_id: finishedSeries, display_title: "Berserk", chapter_id: finishedChapter,
          display_label: "Capítulo 8", page_index: 9, percentage: "100.00", updated_at: "2026-10-16T10:00:00Z"},
        {series_id: ongoingSeries, display_title: "Frieren", chapter_id: ongoingChapter,
          display_label: "Capítulo 2", page_index: 3, percentage: "30.00", updated_at: "2026-10-15T10:00:00Z"},
      ]));
      return Promise.resolve(recentLibraryResponse([]));
    }));
    render(<App />);
    expect(await screen.findByRole("heading", {level: 2, name: "Frieren"})).toBeInTheDocument();
    expect(screen.getByRole("link", {name: "Continuar lectura: Capítulo 2"})).toHaveAttribute("href", `/reader/${ongoingChapter}`);
    expect(screen.getByRole("link", {name: "Ver capítulos de Berserk"})).toHaveAttribute("href", `/series/${finishedSeries}`);
    expect(screen.queryByRole("link", {name: "Leer Capítulo 8 de Berserk"})).not.toBeInTheDocument();
    expect(screen.getByText(/^Capítulo terminado/)).toBeInTheDocument();
    expect(screen.getByRole("link", {name: "Elegir capítulo de Frieren"})).toHaveAttribute("href", `/series/${ongoingSeries}#chapters`);
    expect(screen.getByRole("link", {name: "Abrir Capítulo 8 de Berserk"})).toHaveAttribute("href", `/reader/${finishedChapter}`);
    await screen.findByText("Todavía no hay obras recientes en tu biblioteca.");
    const fetchMock = vi.mocked(fetch);
    const requestsBeforeFiltering = fetchMock.mock.calls.length;
    fireEvent.click(screen.getByRole("button", {name: "En curso"}));
    expect(screen.getByRole("button", {name: "En curso"})).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("link", {name: "Ver capítulos de Berserk"})).not.toBeInTheDocument();
    expect(screen.getByRole("link", {name: "Leer Capítulo 2 de Frieren"})).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Terminados"}));
    expect(screen.getByRole("link", {name: "Ver capítulos de Berserk"})).toBeInTheDocument();
    expect(screen.queryByRole("link", {name: "Leer Capítulo 2 de Frieren"})).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Todo"}));
    expect(screen.getByRole("link", {name: "Leer Capítulo 2 de Frieren"})).toBeInTheDocument();
    expect(fetchMock.mock.calls).toHaveLength(requestsBeforeFiltering);
  });
  it("focuses home search with slash without intercepting typing and submits an encoded query", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/system/readiness") return Promise.resolve(mockResponse({status: "ready"}));
      if (path.startsWith("/api/v1/reader/progress/recent?")) return Promise.resolve(recentReaderProgressResponse([]));
      if (path === "/api/v1/connectors") return Promise.resolve(mockResponse({data: []}));
      return Promise.resolve(recentLibraryResponse([]));
    }));
    render(<App />);
    const input = screen.getByLabelText("Buscar una historia");
    const menu = screen.getByRole("button", {name: "Abrir navegación"});
    menu.focus();
    fireEvent.keyDown(menu, {key: "/", ctrlKey: true});
    expect(menu).toHaveFocus();
    fireEvent.keyDown(menu, {key: "/"});
    expect(input).toHaveFocus();
    const typingSlash = new KeyboardEvent("keydown", {key: "/", bubbles: true, cancelable: true});
    fireEvent(input, typingSlash);
    expect(typingSlash.defaultPrevented).toBe(false);
    fireEvent.change(input, {target: {value: "   "}});
    fireEvent.submit(screen.getByRole("search", {name: "Buscar historias"}));
    expect(window.location.pathname).toBe("/");
    const query = "Berserk & Frieren / Español";
    fireEvent.change(input, {target: {value: `  ${query}  `}});
    fireEvent.submit(screen.getByRole("search", {name: "Buscar historias"}));
    expect(await screen.findByLabelText("Título o palabras clave")).toHaveValue(query);
    expect(window.location.pathname).toBe("/search");
    expect(new URLSearchParams(window.location.search).get("q")).toBe(query);
    expect(window.location.search).toContain("%26");
  });
  it("keeps all desktop destinations, collapses the menu and offers global search from the library", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/system/readiness") return Promise.resolve(mockResponse({status: "ready"}));
      if (path.startsWith("/api/v1/reader/progress/recent?")) return Promise.resolve(recentReaderProgressResponse([]));
      if (path.startsWith("/api/v1/library/recent?")) return Promise.resolve(recentLibraryResponse([]));
      if (path.startsWith("/api/v1/library?")) return Promise.resolve(libraryResponse([]));
      if (path === "/api/v1/library/local-imports") return Promise.resolve(mockResponse({folder: null, scanning: false, imported_chapters: 0, pending_metadata: 0, errors: []}));
      return Promise.resolve(mockResponse({data: []}));
    }));
    render(<App />);
    const nav = screen.getByRole("navigation", {name: "Navegación principal"});
    expect([...nav.querySelectorAll("a")].map((link) => link.getAttribute("href"))).toEqual([
      "/", "/search", "/library", "/downloads", "/usb-exports", "/settings", "/diagnostics",
    ]);
    const toggle = screen.getByRole("button", {name: /^(Expandir|Contraer) menú$/});
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("link", {name: "Yomira · Inicio"})).toHaveAttribute("href", "/");
    const expanded = toggle.getAttribute("aria-expanded");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", expanded === "true" ? "false" : "true");
    fireEvent.click(screen.getByRole("link", {name: "Biblioteca"}));
    expect(await screen.findByRole("heading", {level: 1, name: "Biblioteca"})).toBeInTheDocument();
    const input = screen.getByLabelText("Buscar una historia");
    const explore = screen.getByRole("link", {name: "Explorar"});
    explore.focus();
    fireEvent.keyDown(explore, {key: "/"});
    expect(input).toHaveFocus();
  });
  it("opens a real recent library title from surprise without changing reading progress", async () => {
    const records = [series(syntheticSeriesId(71), "Primera obra"), series(syntheticSeriesId(72), "Obra sorpresa")];
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const path = String(input);
      if (path === "/api/v1/system/readiness") return Promise.resolve(mockResponse({status: "ready"}));
      if (path.startsWith("/api/v1/reader/progress/recent?")) return Promise.resolve(recentReaderProgressResponse([]));
      if (path.startsWith("/api/v1/library/recent?")) return Promise.resolve(recentLibraryResponse(records));
      if (path === `/api/v1/series/${records[1]!.series_id}`) return Promise.resolve(mockResponse({data: records[1], meta: {}}));
      return Promise.resolve(mockResponse({data: [], meta: {limit: 100, offset: 0, total: 0}}));
    });
    vi.stubGlobal("fetch", fetchMock);
    const random = vi.spyOn(Math, "random").mockReturnValue(0.75);
    try {
      render(<App />);
      const surprise = screen.getByRole("button", {name: "Sorpréndeme"});
      expect(surprise).toBeDisabled();
      await waitFor(() => expect(surprise).toBeEnabled());
      fireEvent.click(surprise);
      expect(await screen.findByRole("heading", {level: 1, name: "Obra sorpresa"})).toBeInTheDocument();
      expect(window.location.pathname).toBe(`/series/${records[1]!.series_id}`);
      expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/v1/library?"))).toBe(false);
      expect(fetchMock.mock.calls.every((call) => !["POST", "PUT", "PATCH", "DELETE"].includes(call[1]?.method ?? "GET"))).toBe(true);
    } finally {
      random.mockRestore();
    }
  });
  it("returns from a series to the same library query, filter, order, layout and page", async () => {
    const record = series(syntheticSeriesId(14), "Manga guardado");
    const returnTo = "/library?view=compact&sort=recent&status=reading&q=Manga&offset=100";
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.startsWith("/api/v1/library?")) return Promise.resolve(libraryResponse([record], 201, 100));
      if (path === `/api/v1/series/${record.series_id}`) return Promise.resolve(mockResponse({data: record, meta: {}}));
      if (path === "/api/v1/library/local-imports") return Promise.resolve(mockResponse({folder: "C:/Mangas", scanning: false, imported_chapters: 0, pending_metadata: 0, errors: []}));
      return Promise.resolve(mockResponse({data: [], meta: {limit: 100, offset: 0, total: 0}}));
    }));
    window.history.replaceState({}, "", returnTo);
    render(<App />);
    fireEvent.click(await screen.findByRole("link", {name: "Manga guardado"}));
    await screen.findByRole("heading", {level: 1, name: "Manga guardado"});
    expect(screen.getByRole("link", {name: "Volver a Biblioteca"})).toHaveAttribute("href", returnTo);
    fireEvent.click(screen.getByRole("link", {name: "Volver a Biblioteca"}));
    expect(await screen.findByRole("heading", {level: 2, name: "Manga guardado"})).toBeInTheDocument();
    expect(screen.getByRole("searchbox", {name: "Buscar en toda la biblioteca"})).toHaveValue("Manga");
    expect(screen.getByRole("button", {name: "Leyendo"})).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", {name: "Vista compacta"})).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("combobox", {name: "Ordenar biblioteca"})).toHaveValue("recent");
    expect(screen.getByText("Página 2 de 3")).toBeInTheDocument();
  });

  it("recovers a stale library page without discarding its search or layout", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.startsWith("/api/v1/library?")) return Promise.resolve(path.includes("offset=100")
        ? libraryResponse([], 1, 100) : libraryResponse([series(syntheticSeriesId(15), "Manga")], 1));
      return Promise.resolve(mockResponse({folder: "C:/Mangas", scanning: false, imported_chapters: 0, pending_metadata: 0, errors: []}));
    }));
    window.history.replaceState({}, "", "/library?offset=100&q=Manga&view=compact");
    render(<App />);
    fireEvent.click(await screen.findByRole("button", {name: "Volver a la primera página"}));
    expect(await screen.findByRole("heading", {level: 2, name: "Manga"})).toBeInTheDocument();
    expect(window.location.search).toContain("q=Manga");
    expect(window.location.search).toContain("view=compact");
    expect(window.location.search).not.toContain("offset=");
  });

  it("changes library layout without fetching and requests global ordering from the server", async () => {
    const first = { ...series(syntheticSeriesId(1), "Zeta"), created_at: "2026-10-10T10:00:00Z" };
    const second = { ...series(syntheticSeriesId(2), "Alfa"), updated_at: "2026-10-11T10:00:00Z" };
    const records = [first, second];
    const fetchMock = vi.fn((input: RequestInfo | URL) => Promise.resolve(
      String(input).startsWith("/api/v1/library?") ? libraryResponse(
        String(input).includes("sort=recent") ? [first, second] : [second, first])
        : mockResponse({ folder: "C:/Mangas", scanning: false, imported_chapters: 0, pending_metadata: 0, errors: [] }),
    ));
    vi.stubGlobal("fetch", fetchMock);
    renderLibrary();
    const list = await screen.findByRole("list", {name: "Series de la biblioteca"});
    expect([...list.querySelectorAll("h2")].map((node) => node.textContent)).toEqual(["Alfa", "Zeta"]);
    const requests = fetchMock.mock.calls.length;
    fireEvent.click(screen.getByRole("button", {name: "Vista compacta"}));
    expect(screen.getByRole("button", {name: "Vista compacta"})).toHaveAttribute("aria-pressed", "true");
    expect(window.location.search).toContain("view=compact");
    expect(fetchMock).toHaveBeenCalledTimes(requests);
    fireEvent.change(screen.getByRole("combobox", {name: "Ordenar biblioteca"}), {target: {value: "recent"}});
    await waitFor(() => expect([...screen.getByRole("list", {name: "Series de la biblioteca"}).querySelectorAll("h2")]
      .map((node) => node.textContent)).toEqual(["Zeta", "Alfa"]));
    expect(window.location.search).toContain("sort=recent");
    fireEvent.change(screen.getByRole("combobox", {name: "Ordenar biblioteca"}), {target: {value: "updated"}});
    await waitFor(() => expect([...screen.getByRole("list", {name: "Series de la biblioteca"}).querySelectorAll("h2")]
      .map((node) => node.textContent)).toEqual(["Alfa", "Zeta"]));
    expect(fetchMock).toHaveBeenCalledTimes(requests + 2);
    expect(records.map((record) => record.display_title)).toEqual(["Zeta", "Alfa"]);
  });

  it("restores library presentation from its URL and ignores unknown presentation values", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(
      String(input).startsWith("/api/v1/library?") ? libraryResponse([series(syntheticSeriesId(1), "Manga")])
        : mockResponse({folder: "C:/Mangas", scanning: false, imported_chapters: 0, pending_metadata: 0, errors: []}),
    )));
    window.history.replaceState({}, "", "/library?view=compact&sort=invalid");
    render(<App />);
    expect(await screen.findByRole("button", {name: "Vista compacta"})).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("combobox", {name: "Ordenar biblioteca"})).toHaveValue("title");
    fireEvent.click(screen.getByRole("button", {name: "Vista de portadas"}));
    expect(screen.getByRole("button", {name: "Vista de portadas"})).toHaveAttribute("aria-pressed", "true");
  });
  it.each([
    ["/library", "Actualizar biblioteca", "/api/v1/library?"],
    [`/series/${syntheticSeriesId(105)}`, "Actualizar metadatos y portada", `/api/v1/series/${syntheticSeriesId(105)}`],
    ["/downloads", "Actualizar", "/api/v1/download-jobs?"],
  ])("keeps controls and content stable during automatic refresh on %s", async (route, buttonName, endpoint) => {
    vi.useFakeTimers();
    const seriesId = syntheticSeriesId(105);
    let holdRefresh = false;
    let resolveRefresh: ((response: Response) => void) | undefined;
    const response = (path: string, updated = false) => {
      const title = updated ? "Título actualizado" : "Título inicial";
      if (path.startsWith("/api/v1/library?")) return libraryResponse([series(seriesId, title)]);
      if (path === `/api/v1/series/${seriesId}`) return mockResponse({data: series(seriesId, title), meta: {}});
      if (path === "/api/v1/library/local-imports") return mockResponse({
        folder: "C:/Mangas", scanning: holdRefresh, imported_chapters: 3, pending_metadata: 0, errors: [],
      });
      if (path.startsWith("/api/v1/download-jobs?")) return mockResponse({
        data: [{job_id: syntheticSeriesId(101), chapter_source_id: syntheticSeriesId(102),
          series_id: seriesId, chapter_id: syntheticSeriesId(104), status: "DOWNLOADING", priority: "NORMAL",
          series_title: title, chapter_label: "Capítulo 1", created_at: "2026-10-07T10:00:00Z",
          updated_at: "2026-10-07T10:01:00Z", cancel_requested_at: null}],
        meta: {limit: 50, offset: 0, total: 1},
      });
      return mockResponse({data: [], meta: {limit: 100, offset: 0, total: 0}});
    };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      return holdRefresh && path.startsWith(endpoint)
        ? new Promise<Response>((resolve) => { resolveRefresh = resolve; })
        : Promise.resolve(response(path));
    }));
    window.history.replaceState({}, "", route);
    const view = render(<App />);
    await act(async () => {
      await vi.dynamicImportSettled();
      await vi.advanceTimersByTimeAsync(350);
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    if (route.startsWith("/series/")) fireEvent.click(screen.getByLabelText("Más acciones del manga"));
    const button = screen.getByRole("button", {name: buttonName});
    const titleNode = route === "/library" ? screen.getByText("Título inicial", {selector: "a"}) : screen.getByRole("heading", {name: "Título inicial"});
    holdRefresh = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(route === "/downloads" ? 2_010 : 15_010); });
    expect(resolveRefresh).toBeDefined();
    expect(button).toBeEnabled();
    expect(button).toHaveTextContent(buttonName);
    expect(titleNode).toBeInTheDocument();
    if (route === "/library") expect(screen.queryByText(/Comprobando archivos/)).not.toBeInTheDocument();
    resolveRefresh!(response(endpoint, true));
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(route === "/library" ? screen.getByText("Título actualizado", {selector: "a"}) : screen.getByRole("heading", {name: "Título actualizado"})).toBeInTheDocument();
    if (route === "/downloads") {
      fireEvent.click(button);
      await act(async () => { await vi.advanceTimersByTimeAsync(10); });
      expect(button).toBeDisabled();
      expect(button).toHaveTextContent("Actualizando…");
      resolveRefresh!(response(endpoint, true));
      await act(async () => { await vi.advanceTimersByTimeAsync(10); });
      expect(button).toBeEnabled();
      expect(button).toHaveTextContent("Actualizar");
    }
    view.unmount();
  });

  it("updates completed downloads automatically without pressing refresh", async () => {
    vi.useFakeTimers();
    let completed = false;
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(mockResponse({
      data: [{
        job_id: syntheticSeriesId(101),
        chapter_source_id: syntheticSeriesId(102),
        series_id: syntheticSeriesId(103),
        chapter_id: syntheticSeriesId(104),
        status: completed ? "COMPLETED" : "DOWNLOADING",
        priority: "NORMAL",
        series_title: "Descarga automática",
        chapter_label: "Capítulo 1",
        created_at: "2026-10-07T10:00:00Z",
        updated_at: "2026-10-07T10:01:00Z",
        cancel_requested_at: null,
      }],
      meta: { limit: 50, offset: 0, total: 1 },
    }))));
    window.history.replaceState({}, "", "/downloads");
    const view = render(<App />);
    await act(async () => {
      await vi.dynamicImportSettled();
      await vi.advanceTimersByTimeAsync(350);
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(screen.getByText("Descargando")).toBeInTheDocument();
    completed = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(2_010); });
    expect(screen.getByText("Completado")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Leer Capítulo 1/ })).toBeInTheDocument();
    view.unmount();
  });

  it("shows a newly indexed chapter automatically and refreshes a cached library on return", async () => {
    vi.useFakeTimers();
    const seriesId = syntheticSeriesId(105);
    const chapterId = syntheticSeriesId(106);
    let ready = false;
    let title = "Título inicial";
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/v1/series/${seriesId}`) {
        return Promise.resolve(mockResponse({ data: series(seriesId, title), meta: {} }));
      }
      if (path.startsWith(`/api/v1/reader/series/${seriesId}/chapters?`)) {
        const chapters = ready ? [{
          chapter_id: chapterId, series_id: seriesId, display_label: "Capítulo 2", page_count: 3,
        }] : [];
        return Promise.resolve(mockResponse({
          data: chapters, meta: { limit: 100, offset: 0, total: chapters.length },
        }));
      }
      return Promise.resolve(libraryResponse([series(seriesId, title)]));
    }));
    const view = renderLibrary();
    await act(async () => {
      await vi.dynamicImportSettled();
      await vi.advanceTimersByTimeAsync(350);
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    fireEvent.click(screen.getByRole("link", { name: title }));
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(screen.getByText(/todavía no tiene capítulos descargados/)).toBeInTheDocument();
    ready = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(15_010); });
    expect(screen.getByRole("link", { name: "Leer Capítulo 2" })).toBeInTheDocument();
    title = "Título actualizado";
    fireEvent.click(screen.getByRole("link", { name: "Volver a Biblioteca" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(screen.getByRole("heading", { level: 2, name: title })).toBeInTheDocument();
    view.unmount();
  });

  it("renders diagnostic checks and keeps unknown distinct from healthy", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/system/liveness") {
        return Promise.resolve(mockResponse({ status: "alive" }));
      }
      if (path === "/api/v1/system/readiness") {
        return Promise.resolve(mockResponse({ status: "ready" }));
      }
      if (path === "/api/v1/system/diagnostics") {
        return Promise.resolve(systemDiagnosticsResponse([
          {
            id: "backend",
            status: "ok",
            code: null,
            message: "La API responde.",
            trace_id: null,
          },
          {
            id: "database",
            status: "warning",
            code: "DB_INTEGRITY_WARNING",
            message: "La base de datos requiere atención.",
            trace_id: "trace-diagnostics-db",
          },
          {
            id: "disk",
            status: "unknown",
            code: null,
            message: "No se pudo consultar el espacio disponible.",
            trace_id: null,
          },
          {
            id: "lan",
            status: "error",
            code: "NET_LAN_UNAVAILABLE",
            message: "La comprobación LAN falló.",
            trace_id: "trace-diagnostics-lan",
          },
        ]));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Abrir navegación" }));
    fireEvent.click(within(screen.getByRole("navigation", { name: "Más opciones" })).getByRole("link", { name: "Diagnóstico" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Diagnóstico" }))
      .toBeInTheDocument();
    expect(await screen.findByText("Yomira 1.2.3 · API 1")).toBeInTheDocument();
    expect(screen.getByText("Esquema de base de datos: 0019_usb_export_history"))
      .toBeInTheDocument();
    expect(screen.getByText("Backend")).toBeInTheDocument();
    expect(screen.getByText("Base de datos")).toBeInTheDocument();
    expect(screen.getByText("Espacio en disco")).toBeInTheDocument();
    expect(screen.getByText("No se pudo consultar el espacio disponible.")).toBeInTheDocument();
    const diskCheck = screen.getByText("No se pudo consultar el espacio disponible.").closest("li");
    expect(diskCheck).not.toBeNull();
    expect(diskCheck).toHaveTextContent("Estado desconocido");
    expect(diskCheck).not.toHaveTextContent("Correcto");
    expect(screen.getByText("DB_INTEGRITY_WARNING")).toBeInTheDocument();
    expect(screen.getByText("trace-diagnostics-db")).toBeInTheDocument();
    expect(screen.getByText("NET_LAN_UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText("trace-diagnostics-lan")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/system/liveness", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/system/readiness", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/system/diagnostics", expect.any(Object));
  });

  it("separates diagnostics loading from an invalid empty check response", async () => {
    let resolveDiagnostics!: (response: Response) => void;
    const pendingDiagnostics = new Promise<Response>((resolve) => {
      resolveDiagnostics = resolve;
    });
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/system/liveness") {
        return Promise.resolve(mockResponse({ status: "alive" }));
      }
      if (path === "/api/v1/system/readiness") {
        return Promise.resolve(mockResponse({ status: "ready" }));
      }
      if (path === "/api/v1/system/diagnostics") return pendingDiagnostics;
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Abrir navegación" }));
    fireEvent.click(within(screen.getByRole("navigation", { name: "Más opciones" })).getByRole("link", { name: "Diagnóstico" }));

    expect(await screen.findByText("Cargando comprobaciones del sistema…"))
      .toBeInTheDocument();
    expect(screen.queryByText("No hay comprobaciones de sistema disponibles."))
      .not.toBeInTheDocument();
    resolveDiagnostics(mockResponse({
      app_version: "1.2.3",
      api_version: 1,
      schema_version: "0019_usb_export_history",
      checks: [],
    }));
    const alert = await screen.findByRole("alert", { name: "Error de diagnóstico" });
    expect(alert).toHaveTextContent("La API devolvió una respuesta de diagnóstico no válida.");
    expect(screen.queryByText("No hay comprobaciones de sistema disponibles."))
      .not.toBeInTheDocument();
  });

  it("shows traced diagnostics failures and retries only after an explicit action", async () => {
    let attempts = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/system/liveness") {
        return Promise.resolve(mockResponse({ status: "alive" }));
      }
      if (path === "/api/v1/system/readiness") {
        return Promise.resolve(mockResponse({ status: "ready" }));
      }
      if (path === "/api/v1/system/diagnostics") {
        attempts += 1;
        return Promise.resolve(attempts === 1
          ? mockResponse({
              error: {
                code: "SYS_DIAGNOSTICS_UNAVAILABLE",
                message: "No se pudo consultar el diagnóstico.",
                retryable: true,
                trace_id: "trace-diagnostics-503",
              },
            }, 503)
          : systemDiagnosticsResponse([{
              id: "backend",
              status: "ok",
              code: null,
              message: "La API responde.",
              trace_id: null,
            }]));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Abrir navegación" }));
    fireEvent.click(within(screen.getByRole("navigation", { name: "Más opciones" })).getByRole("link", { name: "Diagnóstico" }));

    const error = await screen.findByRole("alert", { name: "Error de diagnóstico" });
    expect(error).toHaveTextContent("SYS_DIAGNOSTICS_UNAVAILABLE");
    expect(error).toHaveTextContent("trace-diagnostics-503");
    expect(attempts).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Reintentar diagnóstico" }));
    expect(await screen.findByText("La API responde.")).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it("shows a read-only recovery screen when readiness is not ready", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/system/liveness") {
        return Promise.resolve(mockResponse({ status: "alive" }));
      }
      if (path === "/api/v1/system/readiness") {
        return Promise.resolve(mockResponse({ status: "not_ready" }, 503));
      }
      if (path === "/api/v1/system/diagnostics") {
        return Promise.resolve(systemDiagnosticsResponse([{
          id: "database",
          status: "error",
          code: "DB_INTEGRITY_FAILED",
          message: "La verificación de integridad de SQLite falló.",
          trace_id: "trace-diagnostics-recovery",
        }], null));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Abrir navegación" }));
    fireEvent.click(within(screen.getByRole("navigation", { name: "Más opciones" })).getByRole("link", { name: "Diagnóstico" }));

    expect(await screen.findByRole("heading", { level: 2, name: "Modo de recuperación" }))
      .toBeInTheDocument();
    expect(screen.getByText(/no está listo/i)).toBeInTheDocument();
    expect(screen.getByText("DB_INTEGRITY_FAILED")).toBeInTheDocument();
    expect(screen.getByText("Esquema de base de datos: No disponible")).toBeInTheDocument();
    expect(screen.getByText("trace-diagnostics-recovery")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/system/diagnostics", expect.any(Object));
  });

  it("uses the approved dark sans-serif global foundation", () => {
    render(<App />);

    expect(getComputedStyle(document.documentElement).colorScheme).toBe("dark");
    expect(getComputedStyle(document.body).fontFamily).toContain("Segoe UI");
    expect(getComputedStyle(document.body).fontFamily).toContain("sans-serif");
    expect(getComputedStyle(document.body).margin).toBe("0px");
  });

  it("renders bounded recent library and saved-progress sections with correct destinations", async () => {
    const records = [
      series(syntheticSeriesId(1), "Berserk"),
      series(syntheticSeriesId(2), "Dorohedoro"),
      series(syntheticSeriesId(3), "Frieren"),
      series(syntheticSeriesId(4), "Golden Kamuy"),
    ];
    const progressRecords = [
      {
        series_id: syntheticSeriesId(1),
        display_title: "Berserk",
        chapter_id: syntheticSeriesId(11),
        display_label: "Capítulo 8",
        page_index: 4,
        percentage: "50.00",
        updated_at: "2026-09-28T10:15:00Z",
      },
      {
        series_id: syntheticSeriesId(2),
        display_title: "Dorohedoro",
        chapter_id: syntheticSeriesId(12),
        display_label: "Capítulo 3",
        page_index: 1,
        percentage: "25.00",
        updated_at: "2026-09-28T10:00:00Z",
      },
      {
        series_id: syntheticSeriesId(3),
        display_title: "Frieren",
        chapter_id: syntheticSeriesId(13),
        display_label: "Capítulo 12",
        page_index: 0,
        percentage: "0.00",
        updated_at: "2026-09-27T10:00:00Z",
      },
      {
        series_id: syntheticSeriesId(4),
        display_title: "Golden Kamuy",
        chapter_id: syntheticSeriesId(14),
        display_label: "Capítulo 5",
        page_index: 9,
        percentage: "90.00",
        updated_at: "2026-09-26T10:00:00Z",
      },
    ];
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/system/readiness") {
        return Promise.resolve(mockResponse({ status: "ready" }));
      }
      if (url === "/api/v1/library/recent?limit=4") {
        return Promise.resolve(recentLibraryResponse(records));
      }
      if (url === "/api/v1/reader/progress/recent?limit=4&per_series=true") {
        return Promise.resolve(recentReaderProgressResponse(progressRecords));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByRole("heading", { level: 2, name: "Berserk" }))
      .toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Continuar lectura: Capítulo 8" })).toHaveAttribute(
      "href", `/reader/${syntheticSeriesId(11)}`,
    );
    expect(await screen.findByText("Servicio local listo.")).toBeInTheDocument();
    expect(screen.getByRole("search", { name: "Buscar historias" })).toContainElement(screen.getByLabelText("Buscar una historia"));
    expect(screen.getByRole("navigation", { name: "Navegación principal" }).closest("header"))
      .toContainElement(screen.getByText("Servicio local listo."));
    expect(screen.getByText("Servicio local listo.").parentElement).toContainElement(screen.getByText("Biblioteca local"));
    expect(screen.getByRole("search", { name: "Buscar historias" })).not.toContainElement(screen.getByText("Servicio local listo."));
    expect(screen.getByRole("heading", { level: 2, name: "Mi biblioteca" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Continuar leyendo" }))
      .toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Berserk" })).toHaveAttribute(
      "href",
      `/series/${syntheticSeriesId(1)}`,
    );
    expect(await screen.findByRole("link", { name: "Leer Capítulo 8 de Berserk" })).toHaveAttribute(
      "href",
      `/reader/${syntheticSeriesId(11)}`,
    );
    expect(screen.getByRole("link", { name: "Ver serie Berserk" })).toHaveAttribute(
      "href",
      `/series/${syntheticSeriesId(1)}`,
    );
    const poster = screen.getByRole("link", { name: "Abrir ficha destacada de Berserk" });
    vi.spyOn(poster, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 200, 300));
    vi.stubGlobal("PointerEvent", class extends MouseEvent {
      pointerType: string;
      constructor(type: string, init: PointerEventInit) { super(type, init); this.pointerType = init.pointerType ?? ""; }
    });
    fireEvent.pointerMove(poster, { pointerType: "touch", clientX: 200, clientY: 0 });
    expect(poster.style.getPropertyValue("--tilt-x")).toBe("");
    fireEvent.pointerMove(poster, { pointerType: "mouse", clientX: 200, clientY: 0 });
    expect(poster.style.getPropertyValue("--tilt-x")).toBe("8deg");
    expect(poster.style.getPropertyValue("--tilt-y")).toBe("8deg");
    fireEvent.pointerLeave(poster);
    expect(poster.style.getPropertyValue("--tilt-x")).toBe("");
    expect(poster.style.getPropertyValue("--tilt-y")).toBe("");
    expect(screen.getByText("Capítulo 8 · Página 5 · 50.00 %")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", {name: "Progreso de Capítulo 8"})).toHaveAttribute("value", "50");
    expect(screen.getByRole("progressbar", {name: "Progreso de Capítulo 8"})).toHaveAttribute("max", "100");
    expect(screen.getByRole("progressbar", { name: "Progreso de Capítulo 8 de Berserk" })).toHaveAttribute("value", "50");
    fireEvent.click(screen.getByRole("button", {name: "Terminados"}));
    expect(screen.getByText("No hay capítulos terminados entre tus lecturas recientes.")).toBeInTheDocument();
    expect(screen.queryByRole("link", {name: "Leer Capítulo 8 de Berserk"})).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Todo"}));
    fireEvent.error(screen.getByRole("img", { name: "Portada de Berserk" }));
    expect(screen.getByRole("img", { name: "Portada no disponible: Berserk" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/library/recent?limit=4",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/reader/progress/recent?limit=4&per_series=true",
      expect.any(Object),
    );
    expect(fetchMock.mock.calls.some(([input]) =>
      String(input).includes("/api/v1/library?") || String(input).includes("offset=")
    )).toBe(false);

    fireEvent.click(screen.getByRole("link", { name: "Leer Capítulo 8 de Berserk" }));
    expect(await screen.findByRole("heading", { name: "Capítulo no disponible" }))
      .toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Volver a/ })).toHaveAttribute(
      "href",
      `/series/${syntheticSeriesId(1)}`,
    );
    fireEvent.click(screen.getByRole("link", { name: /Volver a/ }));
    fireEvent.click(await screen.findByRole("link", { name: "Inicio" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) =>
      String(input) === "/api/v1/reader/progress/recent?limit=4&per_series=true"
    )).toHaveLength(2));
  });

  it("shows independent empty states for recent library and saved progress", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/system/readiness") {
        return Promise.resolve(mockResponse({ status: "ready" }));
      }
      if (url === "/api/v1/library/recent?limit=4") {
        return Promise.resolve(recentLibraryResponse([]));
      }
      if (url === "/api/v1/reader/progress/recent?limit=4&per_series=true") {
        return Promise.resolve(recentReaderProgressResponse([]));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText("Todavía no hay obras recientes en tu biblioteca."))
      .toBeInTheDocument();
    expect(screen.getByText("El progreso guardado de tus capítulos aparecerá aquí."))
      .toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Sorpréndeme"})).toBeDisabled();
    expect(screen.getAllByRole("link", { name: "Buscar obras" })).toHaveLength(2);
    expect(screen.queryByRole("link", { name: /Leer .* de / })).not.toBeInTheDocument();
  });

  it("shows a failed readiness recheck instead of retaining the cached ready badge", async () => {
    let readinessAttempts = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/system/readiness") {
        readinessAttempts += 1;
        return Promise.resolve(readinessAttempts === 1 ? mockResponse({status: "ready"}) : mockResponse({error: {
          code: "SYS_READINESS_UNAVAILABLE", message: "No se pudo volver a comprobar el servicio.",
          retryable: true, trace_id: "trace-readiness-recheck",
        }}, 500));
      }
      if (path.startsWith("/api/v1/library/recent?")) return Promise.resolve(recentLibraryResponse([series(syntheticSeriesId(81), "Obra guardada")]));
      if (path.startsWith("/api/v1/reader/progress/recent?")) return Promise.resolve(recentReaderProgressResponse([]));
      return Promise.resolve(mockResponse({data: []}));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    expect(await screen.findByText("Servicio local listo.")).toBeInTheDocument();
    await screen.findByRole("link", {name: "Obra guardada"});
    await screen.findByText("El progreso guardado de tus capítulos aparecerá aquí.");
    expect(screen.getByRole("button", {name: "Sorpréndeme"})).toBeEnabled();
    expect(fetchMock.mock.calls.filter(([path]) => String(path).startsWith("/api/v1/library/recent?"))).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([path]) => String(path).startsWith("/api/v1/reader/progress/recent?"))).toHaveLength(1);
    fireEvent.click(screen.getByRole("link", {name: "Explorar"}));
    await screen.findByRole("heading", {level: 1, name: "Buscar"});
    const clock = vi.spyOn(Date, "now").mockReturnValue(Date.now() + 31_000);
    try {
      fireEvent.click(screen.getByRole("link", {name: "Inicio"}));
      const alert = await screen.findByRole("alert", {name: "No se pudo comprobar el servicio local"});
      expect(alert).toHaveTextContent("trace-readiness-recheck");
      expect(readinessAttempts).toBe(2);
      expect(screen.queryByText("Servicio local listo.")).not.toBeInTheDocument();
      expect(screen.getByRole("button", {name: "Sorpréndeme"})).toBeDisabled();
    } finally {
      clock.mockRestore();
    }
  });

  it("reports recent-section errors independently and retries only the failed section", async () => {
    let libraryAttempts = 0;
    let progressAttempts = 0;
    let finishLibraryRetry: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/system/readiness") {
        return Promise.resolve(mockResponse({ status: "ready" }));
      }
      if (url === "/api/v1/library/recent?limit=4") {
        libraryAttempts += 1;
        if (libraryAttempts === 1) {
          return Promise.resolve(mockResponse({
              error: {
                code: "LIB_STORAGE_UNAVAILABLE",
                message: "No se pudo consultar la biblioteca reciente.",
                retryable: true,
                trace_id: "trace-home-recent-library",
              },
            }, 503));
        }
        return new Promise<Response>((resolve) => {
          finishLibraryRetry = resolve;
        });
      }
      if (url === "/api/v1/reader/progress/recent?limit=4&per_series=true") {
        progressAttempts += 1;
        return Promise.resolve(mockResponse({
          error: {
            code: "READ_PROGRESS_UNAVAILABLE",
            message: "No se pudo consultar el progreso reciente.",
            retryable: true,
            trace_id: "trace-home-recent-progress",
          },
        }, 503));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText("Servicio local listo.")).toBeInTheDocument();
    const libraryAlert = await screen.findByRole("alert", { name: "Error en biblioteca reciente" });
    const progressAlert = await screen.findByRole("alert", { name: "Error en progreso reciente" });
    expect(libraryAlert).toHaveTextContent("LIB_STORAGE_UNAVAILABLE");
    expect(libraryAlert).toHaveTextContent("trace-home-recent-library");
    expect(progressAlert).toHaveTextContent("READ_PROGRESS_UNAVAILABLE");
    expect(progressAlert).toHaveTextContent("trace-home-recent-progress");
    expect(screen.getByRole("link", { name: "Buscar obras" })).toHaveAttribute("href", "/search");

    const retryButton = screen.getByRole("button", { name: "Reintentar biblioteca reciente" });
    fireEvent.click(retryButton);
    expect(await screen.findByText("Cargando obras recientes…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reintentar biblioteca reciente" }))
      .not.toBeInTheDocument();
    expect(progressAttempts).toBe(1);
    finishLibraryRetry?.(
      recentLibraryResponse([series(syntheticSeriesId(21), "Golden Kamuy")]),
    );
    expect(await screen.findByRole("link", { name: "Golden Kamuy" })).toHaveAttribute(
      "href",
      `/series/${syntheticSeriesId(21)}`,
    );
    expect(screen.getByRole("alert", { name: "Error en progreso reciente" }))
      .toHaveTextContent("trace-home-recent-progress");
    expect(libraryAttempts).toBe(2);
    expect(progressAttempts).toBe(1);
  });

  it("waits for readiness and allows a bounded retry before loading the library", async () => {
    let readinessAttempts = 0;
    const fetchMock = vi.fn((input:RequestInfo|URL)=>{
      const path=String(input);
      if(path==="/api/v1/system/readiness") return Promise.resolve(++readinessAttempts===1 ? mockResponse({status:"not_ready"},503) : mockResponse({status:"ready"}));
      if(path.startsWith("/api/v1/library/recent?")) return Promise.resolve(recentLibraryResponse([]));
      if(path.startsWith("/api/v1/reader/progress/recent?")) return Promise.resolve(recentReaderProgressResponse([]));
      return Promise.resolve(mockResponse({data:[],meta:{provider:"MangaDex",criterion:"followed_count",status:"ready",updated_at:"2026-10-21T12:00:00Z",retry_at:null}}));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText("El servicio local no está listo."))
      .toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ver diagnóstico" })).toHaveAttribute(
      "href",
      "/diagnostics",
    );
    expect(screen.getByRole("link", { name: "Buscar obras" })).toHaveAttribute("href", "/search");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/system/readiness", expect.any(Object));

    fireEvent.click(screen.getByRole("button", { name: "Comprobar de nuevo" }));

    expect(await screen.findByText("Servicio local listo.")).toBeInTheDocument();
    expect(await screen.findByText("Todavía no hay obras recientes en tu biblioteca."))
      .toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(5);
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/system/readiness", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/library/recent?limit=4",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/reader/progress/recent?limit=4&per_series=true",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/recommendations",expect.any(Object));
  });

  it("renders the dark app shell and all primary destinations", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1, name: "Descubre tu próxima historia" }))
      .toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "Navegación principal" });
    for (const label of ["Inicio", "Biblioteca", "Explorar", "Cola de descargas", "Exportar USB", "Configuración", "Diagnóstico"]) {
      expect(within(navigation).getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("navigates to the library without losing the shared shell", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(libraryResponse([])));
    render(<App />);

    fireEvent.click(screen.getByRole("link", { name: "Biblioteca" }));

    expect(screen.getByRole("heading", { level: 1, name: "Biblioteca" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Navegación principal" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Biblioteca" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText("Tu biblioteca está vacía.")).toBeInTheDocument();
  });

  it("opens the persistent downloads page from the main navigation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockResponse({ data: [], meta: { limit: 50, offset: 0, total: 0 } }),
      ),
    );
    render(<App />);

    fireEvent.click(screen.getByRole("link", { name: "Cola de descargas" }));

    expect(screen.getByRole("heading", { level: 1, name: "Descargas" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "La cola está vacía" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Cola de descargas" })).toHaveAttribute("aria-current", "page");
  });

  it("opens reader resource settings from the main navigation", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse({
      data: {
        cache_limit_mb: 256,
        preload_ahead: 1,
        mode: "double",
        rtl: true,
        e_reader: false,
      },
      meta: {},
    })));
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Abrir navegación" }));
    fireEvent.click(screen.getByRole("link", { name: "Configuración" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Ajustes" }))
      .toBeInTheDocument();
    expect(await screen.findByRole("spinbutton", { name: /presupuesto de caché/i }))
      .toHaveValue(256);
    fireEvent.click(screen.getByRole("button", { name: "Abrir navegación" }));
    expect(screen.getByRole("link", { name: "Configuración" })).toHaveAttribute("aria-current", "page");
  });

  it("renders validated local covers and an accessible placeholder after an image failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        libraryResponse([
          series("123e4567-e89b-42d3-a456-426614174000", "Berserk"),
          series("123e4567-e89b-42d3-a456-426614174001", "One Piece", "123e4567-e89b-42d3-a456-426614174002"),
        ]),
      ),
    );
    renderLibrary();

    expect(await screen.findByRole("heading", { level: 2, name: "Berserk" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "One Piece" })).toBeInTheDocument();
    expect(screen.getByText("Sin portada asignada")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir One Piece" })).toHaveAttribute(
      "href", "/series/123e4567-e89b-42d3-a456-426614174001",
    );
    const cover = screen.getByRole("img", { name: "Portada de One Piece" });
    const coverUrl = new URL(cover.getAttribute("src") ?? "", "http://127.0.0.1:8000");
    expect(coverUrl.pathname).toBe("/api/v1/series/123e4567-e89b-42d3-a456-426614174001/cover");
    expect(coverUrl.searchParams.get("variant")).toBe("medium");
    expect(coverUrl.searchParams.get("v"))
      .toBe("123e4567-e89b-42d3-a456-426614174002:2026-09-26T10:00:00Z");
    expect(cover).toHaveAttribute("loading", "lazy");
    expect(screen.queryByText("Vista previa no disponible")).not.toBeInTheDocument();
    fireEvent.error(cover);
    expect(screen.getByRole("img", { name: "Vista previa no disponible: One Piece" }))
      .not.toHaveAttribute("src");
    expect(screen.getByText("Vista previa no disponible")).toBeInTheDocument();
    expect(screen.getByText("Mostrando 1–2 de 2 series")).toBeInTheDocument();
  });

  it("exposes only reader-ready downloaded chapters from the series detail", async () => {
    const seriesId = syntheticSeriesId(90);
    const chapterId = syntheticSeriesId(91);
    const chapterListPath = `/api/v1/reader/series/${seriesId}/chapters?limit=100&offset=0`;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/v1/series/${seriesId}`) {
        return Promise.resolve(mockResponse({ data: series(seriesId, "Mock Fixture Series"), meta: {} }));
      }
      if (path === chapterListPath) {
        return Promise.resolve(mockResponse({
          data: [{
            chapter_id: chapterId,
            series_id: seriesId,
            display_label: "Capítulo 1",
            page_count: 3,
          }],
          meta: { limit: 100, offset: 0, total: 1 },
        }));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/series/${seriesId}`);

    render(<App />);

    expect(await screen.findByRole("heading", { level: 2, name: "Capítulos listos para leer" }))
      .toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Leer Capítulo 1" })).toHaveAttribute(
      "href",
      `/reader/${chapterId}`,
    );
    expect(screen.getByText("3 páginas")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(chapterListPath, expect.any(Object));
  });

  it("returns to the originating series when its reader chapter is unavailable", async () => {
    const seriesId = syntheticSeriesId(98);
    const chapterId = syntheticSeriesId(99);
    const chapterListPath = `/api/v1/reader/series/${seriesId}/chapters?limit=100&offset=0`;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/v1/series/${seriesId}`) {
        return Promise.resolve(mockResponse({ data: series(seriesId, "Unavailable Series"), meta: {} }));
      }
      if (path === chapterListPath) {
        return Promise.resolve(mockResponse({
          data: [{
            chapter_id: chapterId,
            series_id: seriesId,
            display_label: "Capítulo 1",
            page_count: 3,
          }],
          meta: { limit: 100, offset: 0, total: 1 },
        }));
      }
      if (path === `/api/v1/reader/${chapterId}`) {
        return Promise.resolve(mockResponse({
          error: {
            code: "READ_ASSET_NOT_FOUND",
            message: "El capítulo no está disponible.",
            retryable: false,
            trace_id: "trace-reader-series-return-404",
          },
        }, 404));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/series/${seriesId}`);

    render(<App />);

    fireEvent.click(await screen.findByRole("link", { name: "Leer Capítulo 1" }));
    expect(await screen.findByRole("heading", { name: "Capítulo no disponible" })).toBeInTheDocument();
    expect(screen.getByText("trace-reader-series-return-404")).toBeInTheDocument();
    const returnLink = screen.getByRole("link", { name: /Volver a/ });
    expect(returnLink).toHaveAttribute("href", `/series/${seriesId}`);
  });

  it("paginates reader-ready chapters and disables navigation at the reported bounds", async () => {
    const seriesId = syntheticSeriesId(92);
    const firstChapterId = syntheticSeriesId(93);
    const lastChapterId = syntheticSeriesId(94);
    const firstPagePath = `/api/v1/reader/series/${seriesId}/chapters?limit=100&offset=0`;
    const secondPagePath = `/api/v1/reader/series/${seriesId}/chapters?limit=100&offset=100`;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/v1/series/${seriesId}`) {
        return Promise.resolve(mockResponse({ data: series(seriesId, "Paged Series"), meta: {} }));
      }
      if (path === firstPagePath) {
        return Promise.resolve(mockResponse({
          data: [{
            chapter_id: firstChapterId,
            series_id: seriesId,
            display_label: "Capítulo 1",
            page_count: 3,
          }],
          meta: { limit: 100, offset: 0, total: 101 },
        }));
      }
      if (path === secondPagePath) {
        return Promise.resolve(mockResponse({
          data: [{
            chapter_id: lastChapterId,
            series_id: seriesId,
            display_label: "Capítulo 101",
            page_count: 4,
          }],
          meta: { limit: 100, offset: 100, total: 101 },
        }));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/series/${seriesId}`);

    render(<App />);

    expect(await screen.findByRole("link", { name: "Leer Capítulo 1" })).toHaveAttribute(
      "href",
      `/reader/${firstChapterId}`,
    );
    expect(screen.getByText("Página 1 de 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Anterior" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Siguiente" }));

    expect(await screen.findByRole("link", { name: "Leer Capítulo 101" })).toHaveAttribute(
      "href",
      `/reader/${lastChapterId}`,
    );
    expect(screen.getByText("Página 2 de 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Siguiente" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Anterior" }));

    expect(await screen.findByRole("link", { name: "Leer Capítulo 1" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(firstPagePath, expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith(secondPagePath, expect.any(Object));
  });

  it("shows traced chapter-list failures and loads the page only after a retry", async () => {
    const seriesId = syntheticSeriesId(95);
    const chapterId = syntheticSeriesId(96);
    const chapterListPath = `/api/v1/reader/series/${seriesId}/chapters?limit=100&offset=0`;
    let chapterAttempts = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/v1/series/${seriesId}`) {
        return Promise.resolve(mockResponse({ data: series(seriesId, "Recoverable Series"), meta: {} }));
      }
      if (path === chapterListPath) {
        chapterAttempts += 1;
        if (chapterAttempts === 1) {
          return Promise.resolve(mockResponse({
            error: {
              code: "SYS_INTERNAL_ERROR",
              message: "No se pudo consultar la biblioteca local.",
              retryable: true,
              trace_id: "trace-reader-chapters-503",
            },
          }, 503));
        }
        return Promise.resolve(mockResponse({
          data: [{
            chapter_id: chapterId,
            series_id: seriesId,
            display_label: "Capítulo 1",
            page_count: 2,
          }],
          meta: { limit: 100, offset: 0, total: 1 },
        }));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/series/${seriesId}`);

    render(<App />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("SYS_INTERNAL_ERROR");
    expect(alert).toHaveTextContent("trace-reader-chapters-503");
    expect(chapterAttempts).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));

    expect(await screen.findByRole("link", { name: "Leer Capítulo 1" })).toHaveAttribute(
      "href",
      `/reader/${chapterId}`,
    );
    expect(chapterAttempts).toBe(2);
  });

  it("offers search when the series has no reader-ready chapter", async () => {
    const seriesId = syntheticSeriesId(97);
    const chapterListPath = `/api/v1/reader/series/${seriesId}/chapters?limit=100&offset=0`;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/v1/series/${seriesId}`) {
        return Promise.resolve(mockResponse({ data: series(seriesId, "Empty Series"), meta: {} }));
      }
      if (path === chapterListPath) {
        return Promise.resolve(mockResponse({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/series/${seriesId}`);

    render(<App />);

    expect(await screen.findByText(
      "Esta serie todavía no tiene capítulos descargados y verificados para leer.",
    )).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Buscar capítulos" })).toHaveAttribute(
      "href",
      "/search?q=Empty+Series",
    );
  });

  it("searches the full library on submit and distinguishes no matches from an empty library", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => Promise.resolve(String(input).includes("q=naruto")
        ? libraryResponse([]) : libraryResponse([
          series("123e4567-e89b-42d3-a456-426614174000", "Berserk"),
          series("123e4567-e89b-42d3-a456-426614174001", "One Piece"),
        ], 102))),
    );
    renderLibrary();

    await screen.findByRole("heading", { level: 2, name: "Berserk" });
    fireEvent.change(screen.getByRole("searchbox", { name: "Buscar en toda la biblioteca" }), {
      target: { value: "naruto" },
    });

    expect(screen.getByRole("heading", {level: 2, name: "Berserk"})).toBeInTheDocument();
    fireEvent.submit(screen.getByRole("search", {name: "Buscar en la biblioteca"}));
    expect(await screen.findByText("No hay coincidencias en la biblioteca.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2, name: "Berserk" })).not.toBeInTheDocument();
    expect(screen.queryByText("Tu biblioteca está vacía.")).not.toBeInTheDocument();
  });

  it("loads the next bounded page and reports the actual range", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      return path.includes("offset=100")
        ? Promise.resolve(
            libraryResponse(
              [series("123e4567-e89b-42d3-a456-426614174001", "One Piece")],
              101,
              100,
            ),
          )
        : Promise.resolve(
            libraryResponse(
              [series("123e4567-e89b-42d3-a456-426614174000", "Berserk")],
              101,
              0,
            ),
          );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibrary();

    await screen.findByRole("heading", { level: 2, name: "Berserk" });
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));

    expect(await screen.findByRole("heading", { level: 2, name: "One Piece" })).toBeInTheDocument();
    expect(screen.getByText("Mostrando 101–101 de 101 series")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/library?limit=100&offset=100&sort=title",
      { headers: { Accept: "application/json" } },
    ));
  });

  it("keeps a 10,000-series library bounded to one rendered page", async () => {
    const total = 10_000;
    const recordsById = new Map<string, SeriesRecord>();
    const recordFor = (index: number) => {
      const id = syntheticSeriesId(index);
      const record = series(id, `Synthetic Series ${String(index).padStart(5, "0")}`);
      recordsById.set(id, record);
      return record;
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), window.location.origin);
      if (url.pathname === "/api/v1/system/readiness") return Promise.resolve(mockResponse({status: "ready"}));
      if (url.pathname === "/api/v1/library/recent") return Promise.resolve(recentLibraryResponse([]));
      if (url.pathname === "/api/v1/library/local-imports") return Promise.resolve(mockResponse({folder: null, scanning: false, imported_chapters: 0, pending_metadata: 0, errors: []}));
      if (url.pathname.startsWith("/api/v1/series/")) {
        const id = url.pathname.split("/").at(-1);
        const record = id === undefined ? undefined : recordsById.get(id);
        return Promise.resolve(
          record === undefined
            ? mockResponse(null, 404)
            : mockResponse({ data: record, meta: {} }),
        );
      }

      const offset = Number(url.searchParams.get("offset") ?? "0");
      const search = url.searchParams.get("q");
      if (search) return Promise.resolve(libraryResponse([recordFor(Number(search))], 1));
      const data = Array.from(
        { length: Math.min(100, total - offset) },
        (_unused, index) => recordFor(offset + index),
      );
      return Promise.resolve(libraryResponse(data, total, offset));
    });
    vi.stubGlobal("fetch", fetchMock);

    const renderStartedAt = performance.now();
    renderLibrary();
    await screen.findByRole("heading", { level: 2, name: "Synthetic Series 00000" });
    const renderMs = performance.now() - renderStartedAt;
    expect(screen.getAllByRole("heading", { level: 2, name: /Synthetic Series/ })).toHaveLength(100);
    expect(screen.getByRole("link", { name: "Synthetic Series 00099" })).toBeInTheDocument();

    const searchStartedAt = performance.now();
    fireEvent.change(screen.getByRole("searchbox", { name: "Buscar en toda la biblioteca" }), {
      target: { value: "00099" },
    });
    fireEvent.submit(screen.getByRole("search", {name: "Buscar en la biblioteca"}));
    await waitFor(() => expect(screen.getAllByRole("heading", {level: 2, name: /Synthetic Series/})).toHaveLength(1));
    const pageSearchMs = performance.now() - searchStartedAt;
    expect(screen.getAllByRole("heading", { level: 2, name: /Synthetic Series/ })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 2, name: "Synthetic Series 00099" }))
      .toBeInTheDocument();

    fireEvent.change(screen.getByRole("searchbox", { name: "Buscar en toda la biblioteca" }), {
      target: { value: "" },
    });
    fireEvent.submit(screen.getByRole("search", {name: "Buscar en la biblioteca"}));
    await screen.findByRole("heading", {level: 2, name: "Synthetic Series 00000"});
    const pagingStartedAt = performance.now();
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    await screen.findByRole("heading", { level: 2, name: "Synthetic Series 00100" });
    const pagingMs = performance.now() - pagingStartedAt;
    expect(screen.getAllByRole("heading", { level: 2, name: /Synthetic Series/ })).toHaveLength(100);
    expect(screen.getByText("Mostrando 101–200 de 10000 series")).toBeInTheDocument();

    const detailStartedAt = performance.now();
    fireEvent.click(screen.getByRole("link", { name: "Synthetic Series 00100" }));
    await screen.findByRole("heading", { level: 1, name: "Synthetic Series 00100" });
    const detailMs = performance.now() - detailStartedAt;
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/series/${syntheticSeriesId(100)}`, {
      headers: { Accept: "application/json" },
    });

    // eslint-disable-next-line no-console -- K058 records local timing evidence for the phase report.
    console.info(
      `K058_UI_GATE total=${total} page=100 render_ms=${renderMs.toFixed(2)} ` +
        `page_search_ms=${pageSearchMs.toFixed(2)} paging_ms=${pagingMs.toFixed(2)} ` +
        `detail_ms=${detailMs.toFixed(2)}`,
    );
  }, 15_000); // Four accessibility-heavy page transitions in jsdom need headroom on Windows.

  it("resets pagination when the reading filter changes", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("reading_status=completed")) {
        return Promise.resolve(
          libraryResponse([series("123e4567-e89b-42d3-a456-426614174002", "Completed")], 1),
        );
      }
      if (path.includes("offset=100")) {
        return Promise.resolve(
          libraryResponse([series("123e4567-e89b-42d3-a456-426614174001", "One Piece")], 201, 100),
        );
      }
      return Promise.resolve(
        libraryResponse([series("123e4567-e89b-42d3-a456-426614174000", "Berserk")], 201, 0),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibrary();

    await screen.findByRole("heading", { level: 2, name: "Berserk" });
    fireEvent.click(screen.getByRole("button", { name: /siguiente$/i }));
    await screen.findByRole("heading", { level: 2, name: "One Piece" });
    fireEvent.click(screen.getByRole("button", { name: "Completos" }));

    expect(await screen.findByRole("heading", { level: 2, name: "Completed" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2, name: "One Piece" })).not.toBeInTheDocument();
    expect(screen.getByText(/Mostrando 1.*1 de 1 series/)).toHaveAttribute("role", "status");
    expect(screen.getByRole("button", { name: "Completos" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/library?limit=100&offset=0&reading_status=completed&sort=title",
      { headers: { Accept: "application/json" } },
    ));
  });

  it("distinguishes a filter with no results from an empty library", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) =>
      String(input).includes("reading_status=reading")
        ? Promise.resolve(libraryResponse([], 0))
        : Promise.resolve(libraryResponse([series("123e4567-e89b-42d3-a456-426614174000", "Berserk")])),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderLibrary();

    await screen.findByRole("heading", { level: 2, name: "Berserk" });
    fireEvent.click(screen.getByRole("button", { name: "Leyendo" }));

    expect(await screen.findByRole("heading", { level: 2, name: "No hay series con este filtro." }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mostrar todas" })).toBeInTheDocument();
    expect(screen.queryByText("Tu biblioteca est\u00e1 vac\u00eda.")).not.toBeInTheDocument();
  });

  it("opens only a known folder and explains clipboard or folder failures", async () => {
    const inbox = "C:/Mangas de Yomira/Para añadir";
    const clipboard = { writeText: vi.fn().mockRejectedValue(new Error("Permission denied")) };
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: clipboard });
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.startsWith("/api/v1/library?")) return Promise.resolve(libraryResponse([]));
      if (path === "/api/v1/library/local-imports") return Promise.resolve(mockResponse({
        folder: "C:/Mangas de Yomira", scanning: false, imported_chapters: 0,
        pending_metadata: 0, errors: [], folders: [
          { kind: "library", path: "C:/Mangas de Yomira", problem: null },
          { kind: "inbox", path: inbox, problem: null },
          { kind: "downloads", path: "C:/Mangas de Yomira/chapters", problem: "No disponible" },
        ],
      }));
      if (path === "/api/v1/library/folders/inbox/open") return Promise.resolve(mockResponse({
        error: { code: "LIB_PATH_UNAVAILABLE", message: "Copia su ruta y ábrela desde el Explorador.",
          trace_id: "folder-test", retryable: true },
      }, 409));
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibrary();
    expect(await screen.findByText(inbox)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copiar ruta de para añadir mangas" }));
    expect(await screen.findByText(/El navegador no permitió copiar/)).toBeInTheDocument();
    expect(clipboard.writeText).toHaveBeenCalledWith(inbox);
    clipboard.writeText.mockResolvedValueOnce(undefined);
    fireEvent.click(screen.getByRole("button", { name: "Copiar ruta de para añadir mangas" }));
    expect(await screen.findByText(/Ruta copiada/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Abrir para añadir mangas" }));
    expect(await screen.findByText("Copia su ruta y ábrela desde el Explorador.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/library/folders/inbox/open",
      expect.objectContaining({ method: "POST" }));
    expect(screen.getByRole("link", { name: "Gestionar todas las carpetas en Ajustes" })).toBeInTheDocument();
  });

  it("shows the watched manga folder, pending metadata and failed-file retry", async () => {
    let retries = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.startsWith("/api/v1/library?")) return Promise.resolve(libraryResponse([]));
      if (path === "/api/v1/library/local-imports/retry") retries += 1;
      if (path.startsWith("/api/v1/library/local-imports")) return Promise.resolve(mockResponse({
        folder: "C:\\Users\\test\\Documents\\Mangas de Yomira", scanning: false,
        imported_chapters: 1, pending_metadata: retries ? 0 : 1, metadata_review: retries ? 1 : 0,
        errors: retries ? [] : [{ file: "Naruto/Capítulo 002.cbz", message: "Archivo incompleto",
          chapter_id: "11111111-1111-4111-8111-111111111111" }],
      }));
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibrary();
    expect(await screen.findByText("C:\\Users\\test\\Documents\\Mangas de Yomira")).toBeInTheDocument();
    expect(screen.getByText(/Metadatos en cola o en búsqueda: 1 manga/)).toBeInTheDocument();
    expect(screen.getByText("Naruto/Capítulo 002.cbz")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Leer la copia disponible" })).toHaveAttribute("href", "/reader/11111111-1111-4111-8111-111111111111");
    fireEvent.click(screen.getByRole("button", { name: "Reintentar archivos" }));
    await waitFor(() => expect(screen.queryByText("Naruto/Capítulo 002.cbz")).not.toBeInTheDocument());
    expect(await screen.findByText(/La búsqueda terminó. 1 manga/)).toBeInTheDocument();
    expect(screen.queryByText(/Metadatos en cola o en búsqueda/)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/library/local-imports/retry", expect.objectContaining({ method: "POST" }));
  });

  it("keeps the library usable after a traced backend error and retries only on request", async () => {
    let libraryRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/system/readiness") return Promise.resolve(mockResponse({status: "ready"}));
      if (path.startsWith("/api/v1/library/recent?")) return Promise.resolve(recentLibraryResponse([]));
      if (path === "/api/v1/library/local-imports") return Promise.resolve(mockResponse({folder: null, scanning: false, imported_chapters: 0, pending_metadata: 0, errors: []}));
      if (path.startsWith("/api/v1/library?")) {
        libraryRequests += 1;
        return Promise.resolve(libraryRequests === 1 ? mockResponse(
          {
            error: {
              code: "SYS_INTERNAL_ERROR",
              message: "Ocurrió un error interno del sistema.",
              retryable: false,
              trace_id: "trace-library-503",
            },
          },
          503,
        ) : libraryResponse([series("123e4567-e89b-42d3-a456-426614174000", "Berserk")]));
      }
      return Promise.resolve(mockResponse(null, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibrary();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("SYS_INTERNAL_ERROR");
    expect(alert).toHaveTextContent("trace-library-503");
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));

    expect(await screen.findByRole("heading", { level: 2, name: "Berserk" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => String(input).startsWith("/api/v1/library?"))).toHaveLength(2);
  });

  it("opens a series detail and shows only the metadata returned by the API", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    const record = {
      ...series(seriesId, "Berserk"),
      description: "A detailed local description.",
      status: "ongoing",
      language: "ja",
      metadata: series(seriesId, "Berserk").metadata.map((item) => {
        if (item.field === "description") {
          return {
            ...item,
            value: "A detailed local description.",
            local_value: "A detailed local description.",
            authority: "LOCAL_CANONICAL",
          };
        }
        if (item.field === "status") {
          return {
            ...item,
            value: "ongoing",
            local_value: "ongoing",
            authority: "LOCAL_CANONICAL",
          };
        }
        return item;
      }),
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      return String(input).includes(`/series/${seriesId}`)
        ? Promise.resolve(mockResponse({ data: record, meta: {} }))
        : Promise.resolve(libraryResponse([record]));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibrary();

    await screen.findByRole("heading", { level: 2, name: "Berserk" });
    fireEvent.click(screen.getByRole("link", { name: "Berserk" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Berserk" })).toBeInTheDocument();
    expect(screen.getAllByText("A detailed local description.")[0]).toBeInTheDocument();
    fireEvent.click(screen.getByText("Metadatos y procedencia"));
    expect(screen.getByText("En publicación")).toBeInTheDocument();
    expect(screen.getByText("ja")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Volver a Biblioteca" })).toHaveAttribute(
      "href",
      "/library",
    );
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/series/${seriesId}`, {
      headers: { Accept: "application/json" },
    });
  });

  it("resolves a source metadata conflict with an override and restores it when cleared", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    const base = series(seriesId, "Metadata conflict");
    const statusConflict: MetadataRecord = {
      field: "status",
      value: null,
      authority: null,
      local_value: null,
      source_values: [
        {
          series_source_id: "123e4567-e89b-42d3-a456-426614174001",
          source_id: "123e4567-e89b-42d3-a456-426614174002",
          source_display_name: "Source A",
          value: "ongoing",
        },
        {
          series_source_id: "123e4567-e89b-42d3-a456-426614174003",
          source_id: "123e4567-e89b-42d3-a456-426614174004",
          source_display_name: "Source B",
          value: "completed",
        },
      ],
      has_user_override: false,
      user_override_value: null,
      source_conflict: true,
      effective_source_ids: [],
    };
    const conflictRecord: SeriesRecord = {
      ...base,
      metadata: base.metadata.map((item) =>
        item.field === "status" ? statusConflict : item,
      ),
    };
    const overriddenRecord: SeriesRecord = {
      ...conflictRecord,
      metadata: conflictRecord.metadata.map((item) =>
        item.field === "status"
          ? {
              ...item,
              value: "completed",
              authority: "USER_OVERRIDE",
              has_user_override: true,
              user_override_value: "completed",
            }
          : item,
      ),
    };
    let hasOverride = false;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/metadata-overrides")) {
        hasOverride = init?.method === "PUT";
        return Promise.resolve(mockResponse(null, 204));
      }
      if (url.includes(`/series/${seriesId}`)) {
        return Promise.resolve(
          mockResponse({ data: hasOverride ? overriddenRecord : conflictRecord, meta: {} }),
        );
      }
      return Promise.resolve(libraryResponse([conflictRecord]));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibrary();

    await screen.findByRole("heading", { level: 2, name: "Metadata conflict" });
    fireEvent.click(screen.getByRole("link", { name: "Metadata conflict" }));
    await screen.findByText("Conflicto entre fuentes");
    expect(screen.getByText("Source A: En publicación")).toBeInTheDocument();
    fireEvent.click(
        screen.getByRole("button", {
        name: "Usar el valor de Source B para Estado de publicación",
      }),
    );
    await screen.findByText("Corrección local");
    expect(fetchMock.mock.calls).toContainEqual([
      `/api/v1/series/${seriesId}/metadata-overrides`,
      {
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        method: "PUT",
        body: JSON.stringify({ field: "status", value: "completed" }),
      },
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Quitar corrección" }));
    await waitFor(() => expect(screen.getByText("Conflicto entre fuentes")).toBeInTheDocument());
    expect(fetchMock.mock.calls).toContainEqual([
      `/api/v1/series/${seriesId}/metadata-overrides/status`,
      { headers: { Accept: "application/json" }, method: "DELETE" },
    ]);
  });

  it("shows the trace and a safe route back when a series no longer exists", async () => {
    const seriesId = "123e4567-e89b-42d3-a456-426614174000";
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      return String(input).includes(`/series/${seriesId}`)
        ? Promise.resolve(
            mockResponse(
              {
                error: {
                  code: "API_REQUEST_REJECTED",
                  message: "La serie solicitada no existe.",
                  retryable: false,
                  trace_id: "trace-series-404",
                },
              },
              404,
            ),
          )
        : Promise.resolve(libraryResponse([series(seriesId, "Berserk")]));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibrary();

    await screen.findByRole("heading", { level: 2, name: "Berserk" });
    fireEvent.click(screen.getByRole("link", { name: "Berserk" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("API_REQUEST_REJECTED");
    expect(alert).toHaveTextContent("trace-series-404");
    expect(screen.getByRole("link", { name: "Volver a Biblioteca" })).toBeInTheDocument();
  });

  it("opens the compact navigation and closes it after choosing a destination", async () => {
    render(<App />);

    const menuButton = screen.getByRole("button", { name: "Abrir navegación" });
    expect(menuButton).toHaveAttribute("aria-label", "Abrir navegaci\u00f3n");
    fireEvent.click(menuButton);

    expect(menuButton).toHaveAttribute("aria-label", "Cerrar navegaci\u00f3n");
    expect(menuButton).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    const moreNavigation = screen.getByRole("navigation", { name: "Más opciones" });
    expect(within(moreNavigation).getByRole("link", { name: "Exportar USB" })).toHaveAttribute("href", "/usb-exports");
    expect(within(moreNavigation).getByRole("link", { name: "Diagnóstico" })).toHaveAttribute("href", "/diagnostics");

    fireEvent.click(screen.getByRole("link", { name: "Explorar" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Buscar" })).toBeInTheDocument();
    expect(useShellStore.getState().isNavigationOpen).toBe(false);
    expect(moreNavigation).toHaveAttribute("hidden");
    fireEvent.click(menuButton);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(menuButton).toHaveAttribute("aria-expanded", "false");
    expect(menuButton).toHaveFocus();
  });

  it("closes the mobile menu on an outside tap while keeping taps inside available", () => {
    render(<App />);
    const menuButton = screen.getByRole("button", { name: "Abrir navegación" });
    fireEvent.click(menuButton);
    const menu = screen.getByRole("navigation", { name: "Más opciones" });
    fireEvent.pointerDown(menu);
    expect(menuButton).toHaveAttribute("aria-expanded", "true");
    fireEvent.pointerDown(menuButton);
    expect(menuButton).toHaveAttribute("aria-expanded", "true");
    fireEvent.pointerDown(screen.getByRole("main"));
    expect(menuButton).toHaveAttribute("aria-expanded", "false");
    expect(menu).toHaveAttribute("hidden");
    expect(within(screen.getByRole("navigation", { name: "Navegación principal" }))
      .getByRole("link", { name: "Configuración" })).toHaveTextContent("Ajustes");
  });
});
