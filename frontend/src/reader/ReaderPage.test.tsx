import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";

import type { ReaderSettings } from "../api/client";
import { ReaderPage } from "./ReaderPage";

const CHAPTER_ID = "123e4567-e89b-42d3-a456-426614174051";
const SECOND_CHAPTER_ID = "223e4567-e89b-42d3-a456-426614174051";
const SERIES_ID = "123e4567-e89b-42d3-a456-426614174050";
const READER_SETTINGS_PATH = "/api/v1/settings/reader";
const DEFAULT_READER_SETTINGS = {
  cache_limit_mb: 500,
  preload_ahead: 2,
  mode: "single" as const,
  rtl: false,
  e_reader: false,
};

function jsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => payload,
  } as unknown as Response;
}

function pageResponse(status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) => {
        if (name.toLowerCase() === "content-type") return "image/jpeg";
        if (name.toLowerCase() === "content-length") return "3";
        return null;
      },
    },
    json: async () => ({
      error: {
        code: "READ_ASSET_NOT_FOUND",
        message: "La página ya no está disponible.",
        retryable: false,
        trace_id: "trace-reader-page-not-found",
      },
    }),
    blob: async () => new Blob([new Uint8Array([1, 2, 3])], { type: "image/jpeg" }),
    body: null,
  } as unknown as Response;
}

function chapterResponse(status = 200, pageCount = 2, chapterId = CHAPTER_ID): Response {
  return jsonResponse({
    data: {
      chapter_id: chapterId,
      series_id: SERIES_ID,
      display_label: chapterId === CHAPTER_ID ? "Capítulo 8" : "Capítulo 9",
      page_count: pageCount,
    },
    meta: {},
  }, status);
}

function progressResponse(
  pageIndex: number | null,
  percentage = "50.00",
  chapterId = CHAPTER_ID,
): Response {
  return jsonResponse({
    data: pageIndex === null ? null : {
      chapter_id: chapterId,
      series_id: SERIES_ID,
      page_index: pageIndex,
      percentage,
      updated_at: "2026-09-27T15:00:00Z",
    },
    meta: {},
  });
}

function renderReaderPage(
  fetchMock: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
  readerSettings: ReaderSettings | "error" = DEFAULT_READER_SETTINGS,
  navigationState?: unknown,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0, gcTime: 0 } },
  });
  class TestURL extends URL {
    static createObjectURL = vi.fn(() => "blob:reader-page");
    static revokeObjectURL = vi.fn();
  }
  vi.stubGlobal("URL", TestURL);
  const fetchWithReaderSettings = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).startsWith("/api/v1/series/") || String(input).startsWith("/api/v1/reader/series/")) {
      return Promise.resolve(jsonResponse({ data: null, meta: {} }, 404));
    }
    if (String(input) === READER_SETTINGS_PATH && init?.method !== "PUT") {
      return Promise.resolve(readerSettings === "error"
        ? jsonResponse({
          error: {
            code: "API_VALIDATION_FAILED",
            message: "No se pudieron cargar las preferencias.",
            retryable: false,
            trace_id: "trace-reader-settings-failure",
          },
        }, 500)
        : jsonResponse({ data: readerSettings, meta: {} }));
    }
    return fetchMock(input, init);
  });
  vi.stubGlobal("fetch", fetchWithReaderSettings);
  const initialEntry = navigationState === undefined
    ? `/reader/${CHAPTER_ID}`
    : { pathname: `/reader/${CHAPTER_ID}`, state: navigationState };

  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<ReaderPage />} path="/reader/:chapterId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

function viewButton(name: string) {
  if (!screen.queryByRole("button", { name })) fireEvent.click(screen.getByRole("button", { name: "Opciones de lectura" }));
  return screen.getByRole("button", { name });
}

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("ReaderPage", () => {
  it("returns from a reader chapter to its series in the local library", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse());
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/pages/1`) {
        return Promise.resolve(pageResponse());
      }
      return Promise.resolve(jsonResponse({ data: null, meta: {} }, 404));
    });
    renderReaderPage(fetchMock);

    expect(await screen.findByRole("link", { name: "← Volver a la serie" })).toHaveAttribute(
      "href",
      `/series/${SERIES_ID}`,
    );
  });

  it("preserves the series return link while chapter metadata is pending", () => {
    const chapterRequest = new Promise<Response>(() => {});
    const fetchMock = vi.fn((input: RequestInfo | URL) => (
      String(input) === `/api/v1/reader/${CHAPTER_ID}`
        ? chapterRequest
        : Promise.resolve(jsonResponse({ data: null, meta: {} }, 404))
    ));
    renderReaderPage(fetchMock, DEFAULT_READER_SETTINGS, { originatingSeriesId: SERIES_ID });

    expect(screen.getByRole("status")).toHaveTextContent("Cargando información del capítulo");
    expect(screen.getByRole("link", { name: "← Volver a la serie" })).toHaveAttribute(
      "href",
      `/series/${SERIES_ID}`,
    );
  });

  it("creates the first progress row only after the initial image decodes", async () => {
    const putBodies: number[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 1));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        if (init?.method !== "PUT") return Promise.resolve(progressResponse(null));
        putBodies.push((JSON.parse(String(init.body)) as { page_index: number }).page_index);
        return Promise.resolve(progressResponse(putBodies.at(-1) ?? 0, "100.00"));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);

    const image = await screen.findByRole("img", { name: "Capítulo 8, página 1 de 1" });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(`/api/v1/reader/${CHAPTER_ID}/progress`, {
        headers: { Accept: "application/json" },
      });
    });
    expect(putBodies).toEqual([]);

    fireEvent.load(image);
    await waitFor(() => expect(putBodies).toEqual([0]));
  });

  it("does not create initial progress when the visible image fails to decode", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) return Promise.resolve(chapterResponse());
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        if (init?.method === "PUT") return Promise.resolve(progressResponse(0));
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);

    fireEvent.error(await screen.findByRole("img", { name: "Capítulo 8, página 1 de 2" }));
    await screen.findByRole("heading", { name: "No se pudo cargar la página" });
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(0);
  });

  it("restores progress after metadata and does not rewrite a clamped CBZ position", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 3));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        if (init?.method === "PUT") return Promise.resolve(progressResponse(1));
        return Promise.resolve(progressResponse(8));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);

    expect(await screen.findByText("Página 3 de 3")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 3 de 3" }))
      .toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/reader/${CHAPTER_ID}/progress`, {
      headers: { Accept: "application/json" },
    });
    const metadataCallIndex = fetchMock.mock.calls.findIndex(
      ([input]) => String(input) === `/api/v1/reader/${CHAPTER_ID}`,
    );
    const progressCallIndex = fetchMock.mock.calls.findIndex(
      ([input]) => String(input) === `/api/v1/reader/${CHAPTER_ID}/progress`,
    );
    expect(metadataCallIndex).toBeLessThan(progressCallIndex);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Página anterior" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
    });
    expect(fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")?.[1]?.body)
      .toBe(JSON.stringify({ page_index: 1 }));
  });

  it("serializes saves and coalesces pending positions to the latest page", async () => {
    const deferredPuts: {
      first?: (response: Response) => void;
      second?: (response: Response) => void;
    } = {};
    const putBodies: number[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 4));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        if (init?.method !== "PUT") return Promise.resolve(progressResponse(null));
        const body = JSON.parse(String(init.body)) as { page_index: number };
        putBodies.push(body.page_index);
        if (putBodies.length === 1) {
          return new Promise<Response>((resolve) => {
            deferredPuts.first = resolve;
          });
        }
        return new Promise<Response>((resolve) => {
          deferredPuts.second = resolve;
        });
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);

    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 4" });
    const next = screen.getByRole("button", { name: "Página siguiente" });
    fireEvent.click(next);
    await screen.findByText("Página 2 de 4");
    await waitFor(() => expect(putBodies).toEqual([1]));
    expect(screen.getByRole("status")).toHaveTextContent("Guardando progreso");

    fireEvent.click(next);
    await screen.findByText("Página 3 de 4");
    fireEvent.click(next);
    await screen.findByText("Página 4 de 4");
    expect(putBodies).toEqual([1]);
    expect(screen.queryByText("Progreso guardado")).not.toBeInTheDocument();

    expect(deferredPuts.first).toBeDefined();
    deferredPuts.first?.(progressResponse(1));
    await waitFor(() => expect(putBodies).toEqual([1, 3]));
    expect(screen.getByText("Guardando progreso…")).toBeInTheDocument();
    expect(screen.queryByText("Progreso guardado")).not.toBeInTheDocument();
    expect(deferredPuts.second).toBeDefined();
    deferredPuts.second?.(progressResponse(3));
    await screen.findByText("Progreso guardado");
  });

  it("serializes saves by chapter across reader unmount and remount", async () => {
    const putBodies: number[] = [];
    const pendingPuts: Array<(response: Response) => void> = [];
    let persistedPageIndex = 1;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 3));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        if (init?.method !== "PUT") return Promise.resolve(progressResponse(persistedPageIndex));
        const pageIndex = (JSON.parse(String(init.body)) as { page_index: number }).page_index;
        putBodies.push(pageIndex);
        return new Promise<Response>((resolve) => {
          pendingPuts.push((response) => {
            persistedPageIndex = pageIndex;
            resolve(response);
          });
        });
      }
      return Promise.resolve(pageResponse());
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0, gcTime: 0 } },
    });
    class TestURL extends URL {
      static createObjectURL = vi.fn(() => "blob:reader-page");
      static revokeObjectURL = vi.fn();
    }
    vi.stubGlobal("URL", TestURL);
    vi.stubGlobal("fetch", fetchMock);
    function RouteControls() {
      const navigate = useNavigate();
      return (
        <nav>
          <button onClick={() => navigate("/away")} type="button">Salir del lector</button>
          <button onClick={() => navigate(`/reader/${CHAPTER_ID}`)} type="button">
            Volver al mismo capítulo
          </button>
        </nav>
      );
    }

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/reader/${CHAPTER_ID}`]}>
          <RouteControls />
          <Routes>
            <Route element={<ReaderPage />} path="/reader/:chapterId" />
            <Route element={<p>Fuera del lector</p>} path="/away" />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByText("Página 2 de 3");
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    await waitFor(() => expect(putBodies).toEqual([2]));

    fireEvent.click(screen.getByRole("button", { name: "Salir del lector" }));
    await screen.findByText("Fuera del lector");
    fireEvent.click(screen.getByRole("button", { name: "Volver al mismo capítulo" }));
    expect(await screen.findByText("Página 3 de 3")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Página anterior" }));
    await screen.findByText("Página 2 de 3");

    // The older component's page-3 save is still pending. A remounted reader
    // must enqueue its newer page-2 choice behind that request, not race it.
    expect(putBodies).toEqual([2]);
    pendingPuts[0]?.(progressResponse(2));
    await waitFor(() => expect(putBodies).toEqual([2, 1]));
    pendingPuts[1]?.(progressResponse(1));
    await waitFor(() => expect(persistedPageIndex).toBe(1));
  });

  it("shows a traced save failure and retries only after an explicit action", async () => {
    let putCount = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse());
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        if (init?.method !== "PUT") return Promise.resolve(progressResponse(null));
        putCount += 1;
        return Promise.resolve(putCount === 1
          ? jsonResponse({
            error: {
              code: "SYS_INTERNAL_ERROR",
              message: "No se pudo guardar.",
              retryable: true,
              trace_id: "trace-reader-progress-save",
            },
          }, 500)
          : progressResponse(1));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);

    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 2" });
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo guardar el progreso");
    expect(screen.getByText(/trace-reader-progress-save/)).toBeInTheDocument();
    expect(screen.queryByText("Progreso guardado")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reintentar guardado" }));
    await screen.findByText("Progreso guardado");
    expect(putCount).toBe(2);
  });

  it("keeps single-page as the default and bounds the vertical page window", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 2_000));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);

    const singlePageButton = await screen.findByRole("button", { name: "Página única" });
    expect(singlePageButton).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Vertical" }));

    const verticalRegion = screen.getByRole("region", { name: "Páginas en lectura vertical" });
    await screen.findByRole("img", { name: "Capítulo 8, página 3 de 2000" });
    expect(verticalRegion.querySelectorAll("[data-testid='reader-page-slot']")).toHaveLength(3);
    expect(screen.getAllByRole("img").length).toBeLessThanOrEqual(4);

    fireEvent.wheel(verticalRegion, { deltaY: 100 });

    fireEvent.scroll(verticalRegion, { target: { scrollTop: Number.MAX_SAFE_INTEGER } });
    await screen.findByRole("img", { name: "Capítulo 8, página 2000 de 2000" });
    await waitFor(() => {
      expect(verticalRegion.querySelectorAll("[data-testid='reader-page-slot']").length)
        .toBeLessThanOrEqual(4);
    });
  });

  it("aborts requests outside the vertical window and revokes cached URLs on unmount", async () => {
    const pendingSignals = new Map<number, AbortSignal>();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 500));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      const pageMatch = url.match(/\/pages\/(\d+)$/);
      const pageNumber = Number(pageMatch?.[1]);
      if (pageNumber === 2 || pageNumber === 3) {
        const signal = init?.signal;
        if (signal === null || signal === undefined) {
          return Promise.resolve(pageResponse());
        }
        pendingSignals.set(pageNumber, signal);
        return new Promise<Response>((_resolve, reject) => {
          signal.addEventListener(
            "abort",
            () => reject(new DOMException("Request aborted", "AbortError")),
            { once: true },
          );
        });
      }
      return Promise.resolve(pageResponse());
    });
    const view = renderReaderPage(fetchMock);
    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 500" });
    fireEvent.click(screen.getByRole("button", { name: "Vertical" }));
    const verticalRegion = screen.getByRole("region", { name: "Páginas en lectura vertical" });

    await waitFor(() => expect(pendingSignals.size).toBe(2));
    const createObjectURL = vi.mocked(URL.createObjectURL);
    const revokeObjectURL = vi.mocked(URL.revokeObjectURL);
    fireEvent.wheel(verticalRegion, { deltaY: 100 });
    fireEvent.scroll(verticalRegion, { target: { scrollTop: Number.MAX_SAFE_INTEGER } });

    await screen.findByRole("img", { name: "Capítulo 8, página 500 de 500" });
    expect(pendingSignals.get(2)?.aborted).toBe(true);
    expect(pendingSignals.get(3)?.aborted).toBe(true);
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(3));
    expect(revokeObjectURL).not.toHaveBeenCalled();
    expect(verticalRegion.querySelectorAll("[data-testid='reader-page-slot']").length)
      .toBeLessThanOrEqual(4);

    view.unmount();
    expect(revokeObjectURL).toHaveBeenCalledTimes(3);
  });

  it("reuses verified page blobs when navigating back within the cache budget", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 3));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(init?.method === "PUT"
          ? progressResponse((JSON.parse(String(init.body)) as { page_index: number }).page_index)
          : progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    const view = renderReaderPage(fetchMock);

    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 3" });
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    await screen.findByRole("img", { name: "Capítulo 8, página 2 de 3" });
    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    await screen.findByRole("img", { name: "Capítulo 8, página 3 de 3" });
    fireEvent.click(screen.getByRole("button", { name: "Página anterior" }));
    await screen.findByRole("img", { name: "Capítulo 8, página 2 de 3" });
    fireEvent.click(screen.getByRole("button", { name: "Página anterior" }));
    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 3" });

    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/pages/1")))
      .toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/pages/2")))
      .toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/pages/3")))
      .toHaveLength(1);
    view.unmount();
    expect(vi.mocked(URL.revokeObjectURL)).toHaveBeenCalledTimes(3);
  });

  it("keeps vertical page order and reports a failed preload without hiding later pages", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 3));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(url.endsWith("/pages/2") ? pageResponse(404) : pageResponse());
    });
    renderReaderPage(fetchMock);
    fireEvent.click(await screen.findByRole("button", { name: "Vertical" }));

    await screen.findByRole("heading", { name: "Página no encontrada" });
    await screen.findByRole("img", { name: "Capítulo 8, página 3 de 3" });
    const verticalRegion = screen.getByRole("region", { name: "Páginas en lectura vertical" });
    expect(
      Array.from(verticalRegion.querySelectorAll("[data-testid='reader-page-slot']"))
        .map((slot) => slot.getAttribute("aria-label")),
    ).toEqual(["Página 1 de 3", "Página 2 de 3", "Página 3 de 3"]);
    expect(screen.getByText("No se encontró la página 2 de este capítulo.")).toBeInTheDocument();
  });

  it("shows chapter loading feedback before the API responds", async () => {
    let resolveResponse: (value: Response) => void = () => undefined;
    const pendingResponse = new Promise<Response>((resolve) => {
      resolveResponse = resolve;
    });
    const fetchMock = vi.fn((input: RequestInfo | URL) => (
      String(input) === `/api/v1/reader/${CHAPTER_ID}`
        ? pendingResponse
        : Promise.resolve(progressResponse(null))
    ));
    renderReaderPage(fetchMock);

    expect(screen.getByRole("status")).toHaveTextContent("Cargando información del capítulo");
    resolveResponse(chapterResponse());
    expect(await screen.findByRole("heading", { level: 1, name: "Capítulo 8" }))
      .toBeInTheDocument();
  });

  it("loads one verified page at a time and keeps navigation within page_count", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) return Promise.resolve(chapterResponse());
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);

    expect(await screen.findByRole("heading", { level: 1, name: "Capítulo 8" }))
      .toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 1 de 2" }))
      .toHaveAttribute("src", "blob:reader-page");
    const previous = screen.getByRole("button", { name: "Página anterior" });
    const next = screen.getByRole("button", { name: "Página siguiente" });
    expect(previous).toBeDisabled();
    expect(next).toBeEnabled();

    fireEvent.click(next);
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 2 de 2" }))
      .toBeInTheDocument();
    expect(screen.getByText("Página 2 de 2")).toBeInTheDocument();
    expect(next).toBeDisabled();
    expect(previous).toBeEnabled();
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/reader/${CHAPTER_ID}/pages/2`,
      { headers: { Accept: "image/jpeg, image/png, image/webp" }, signal: expect.any(AbortSignal) },
    );

    fireEvent.click(previous);
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 1 de 2" }))
      .toBeInTheDocument();
    expect(previous).toBeDisabled();
  });

  it("navigates double-page spreads without skipping a final unpaired page", async () => {
    const progressWrites: number[] = [];
    const settingsWrites: ReaderSettings[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 5));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        if (init?.method === "PUT") {
          progressWrites.push((JSON.parse(String(init.body)) as { page_index: number }).page_index);
          return Promise.resolve(progressResponse(progressWrites.at(-1) ?? 0));
        }
        return Promise.resolve(progressResponse(null));
      }
      if (url === READER_SETTINGS_PATH && init?.method === "PUT") {
        settingsWrites.push(JSON.parse(String(init.body)) as (typeof settingsWrites)[number]);
        return Promise.resolve(jsonResponse({ data: settingsWrites.at(-1), meta: {} }));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);

    fireEvent.click(await screen.findByRole("button", { name: "Doble página" }));
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 1 de 5" }))
      .toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 2 de 5" }))
      .toBeInTheDocument();
    expect(screen.getByText("Páginas 1–2 de 5")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 3 de 5" }))
      .toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 4 de 5" }))
      .toBeInTheDocument();
    expect(screen.getByText("Páginas 3–4 de 5")).toBeInTheDocument();
    await waitFor(() => expect(progressWrites).toEqual([2]));

    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 5 de 5" }))
      .toBeInTheDocument();
    expect(screen.getByText("Página 5 de 5")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Página siguiente" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Página anterior" }));
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 3 de 5" }))
      .toBeInTheDocument();
    expect(screen.getByText("Páginas 3–4 de 5")).toBeInTheDocument();
    expect(progressWrites).toEqual([2, 4, 2]);
    await waitFor(() => expect(settingsWrites.at(-1)?.mode).toBe("double"));
  });

  it("reverses only the visible spread order in RTL mode", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 4));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock, { ...DEFAULT_READER_SETTINGS, mode: "double", rtl: true });

    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 4" });
    await screen.findByRole("img", { name: "Capítulo 8, página 2 de 4" });
    const displayedPages = screen.getAllByRole("img").map((image) => image.getAttribute("alt"));
    expect(displayedPages).toEqual([
      "Capítulo 8, página 2 de 4",
      "Capítulo 8, página 1 de 4",
    ]);
    expect(viewButton("Derecha a izquierda"))
      .toHaveAttribute("aria-pressed", "true");
  });

  it("bounds manual zoom and exposes fit width, fit height, and reset controls", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) return Promise.resolve(chapterResponse());
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);
    const image = await screen.findByRole("img", { name: "Capítulo 8, página 1 de 2" });

    fireEvent.click(viewButton("Acercar"));
    expect(screen.getByText("125 %")).toBeInTheDocument();
    expect(image).toHaveStyle({ transform: "scale(1.25)" });

    fireEvent.click(viewButton("Ajustar al ancho"));
    expect(viewButton("Ajustar al ancho"))
      .toHaveAttribute("aria-pressed", "true");
    fireEvent.click(viewButton("Ajustar al alto"));
    expect(viewButton("Ajustar al alto"))
      .toHaveAttribute("aria-pressed", "true");

    fireEvent.click(viewButton("Restablecer zoom"));
    expect(screen.getByText("100 %")).toBeInTheDocument();
    expect(viewButton("Ajustar al alto"))
      .toHaveAttribute("aria-pressed", "false");
    expect(viewButton("Acercar")).toBeEnabled();
  });

  it("keeps the reader usable when fullscreen is unsupported", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) return Promise.resolve(chapterResponse());
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);
    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 2" });

    fireEvent.click(viewButton("Pantalla completa"));
    expect(await screen.findByRole("status"))
      .toHaveTextContent("Este navegador no admite pantalla completa para el lector.");
    expect(screen.getByRole("img", { name: "Capítulo 8, página 1 de 2" }))
      .toBeInTheDocument();
  });

  it("uses keyboard navigation in reading order and ignores editable controls", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 3));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(init?.method === "PUT"
          ? progressResponse((JSON.parse(String(init.body)) as { page_index: number }).page_index)
          : progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    const view = renderReaderPage(fetchMock, { ...DEFAULT_READER_SETTINGS, rtl: true });
    const reader = view.container.querySelector("section[aria-labelledby='reader-title']");
    expect(reader).not.toBeNull();

    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 3" });
    fireEvent.keyDown(reader as HTMLElement, { key: "ArrowLeft" });
    await screen.findByRole("img", { name: "Capítulo 8, página 2 de 3" });
    fireEvent.keyDown(reader as HTMLElement, { key: "PageUp" });
    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 3" });

    const input = document.createElement("input");
    reader?.append(input);
    fireEvent.keyDown(input, { key: "ArrowLeft" });
    expect(screen.getByRole("img", { name: "Capítulo 8, página 1 de 3" })).toBeInTheDocument();
  });

  it("uses bounded touch side taps for paged navigation but not center taps or drags", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 3));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);
    const image = await screen.findByRole("img", { name: "Capítulo 8, página 1 de 3" });
    const canvas = image.parentElement as HTMLElement;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, left: 0, top: 0, right: 100, bottom: 100,
      width: 100, height: 100, toJSON: () => ({}),
    });

    fireEvent.pointerDown(canvas, {
      pointerId: 1, pointerType: "touch", clientX: 50, clientY: 50,
    });
    fireEvent.pointerUp(canvas, {
      pointerId: 1, pointerType: "touch", clientX: 50, clientY: 50,
    });
    expect(screen.getByRole("img", { name: "Capítulo 8, página 1 de 3" })).toBeInTheDocument();

    fireEvent.pointerDown(canvas, {
      pointerId: 2, pointerType: "touch", clientX: 95, clientY: 50,
    });
    fireEvent.pointerUp(canvas, {
      pointerId: 2, pointerType: "touch", clientX: 95, clientY: 50,
    });
    await screen.findByRole("img", { name: "Capítulo 8, página 2 de 3" });

    fireEvent.pointerDown(canvas, {
      pointerId: 3, pointerType: "touch", clientX: 95, clientY: 50,
    });
    fireEvent.pointerUp(canvas, {
      pointerId: 3, pointerType: "touch", clientX: 80, clientY: 50,
    });
    expect(screen.getByRole("img", { name: "Capítulo 8, página 2 de 3" })).toBeInTheDocument();
  });

  it("keeps vertical page slots stable when zooming below 100 percent", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) return Promise.resolve(chapterResponse(200, 4));
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      if (url === READER_SETTINGS_PATH && init?.method === "PUT") {
        return Promise.resolve(jsonResponse({
          data: JSON.parse(String(init.body)),
          meta: {},
        }));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock);
    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 4" });
    fireEvent.click(screen.getByRole("button", { name: "Vertical" }));
    const viewport = await screen.findByRole("region", { name: "Páginas en lectura vertical" });

    fireEvent.click(viewButton("Alejar"));
    fireEvent.click(viewButton("Alejar"));

    expect(screen.getByText("50 %")).toBeInTheDocument();
    expect(viewport.style.getPropertyValue("--reader-vertical-page-height"))
      .toBe(viewport.style.getPropertyValue("--reader-page-height"));
  });

  it("persists the e-reader profile and applies a high-contrast reduced-preload presentation", async () => {
    const settingsWrites: ReaderSettings[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 3));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      if (url === READER_SETTINGS_PATH && init?.method === "PUT") {
        const settings = JSON.parse(String(init.body)) as ReaderSettings;
        settingsWrites.push(settings);
        return Promise.resolve(jsonResponse({ data: settings, meta: {} }));
      }
      return Promise.resolve(pageResponse());
    });
    const view = renderReaderPage(fetchMock, {
      ...DEFAULT_READER_SETTINGS,
      mode: "vertical",
      preload_ahead: 2,
    });
    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 3" });
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([input]) => String(input).match(/\/pages\/\d+$/)))
        .toHaveLength(3);
    });
    const profile = await waitFor(() => viewButton("Perfil e-reader"));
    fireEvent.click(profile);

    await waitFor(() => expect(settingsWrites.at(-1)).toMatchObject({
      preload_ahead: 2,
      mode: "vertical",
      e_reader: true,
    }));
    expect(profile).toHaveAttribute("aria-pressed", "true");
    expect(view.container.querySelector("section[aria-labelledby='reader-title']"))
      .toHaveClass(/ereaderProfile/);
    expect(fetchMock.mock.calls.filter(([input]) => String(input).match(/\/pages\/\d+$/)))
      .toHaveLength(3);
  });

  it("starts with no vertical lookahead when the saved e-reader profile is active", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 3));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock, {
      ...DEFAULT_READER_SETTINGS,
      mode: "vertical",
      e_reader: true,
    });

    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 3" });
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([input]) => String(input).match(/\/pages\/\d+$/)))
        .toHaveLength(1);
    });
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/pages/2"))).toBe(false);
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/pages/3"))).toBe(false);
  });

  it("loads the next page when it intersects the viewport with zero preload", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 4));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock, {
      ...DEFAULT_READER_SETTINGS,
      mode: "vertical",
      e_reader: true,
    });

    await screen.findByRole("img", { name: "Capítulo 8, página 1 de 4" });
    const viewport = screen.getByRole("region", { name: "Páginas en lectura vertical" });
    Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 553 });
    fireEvent.click(viewButton("Alejar"));
    fireEvent.click(viewButton("Alejar"));
    fireEvent.wheel(viewport, { deltaY: 100 });
    fireEvent.scroll(viewport, { target: { scrollTop: 200 } });

    expect(await screen.findByRole("img", { name: "Capítulo 8, página 2 de 4" }))
      .toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/pages/2")))
      .toBe(true);
  });

  it("aborts an old chapter page request and releases only the new chapter URL", async () => {
    let oldPageSignal: AbortSignal | undefined;
    let resolveOldPage: ((response: Response) => void) | undefined;
    const createUrlMock = vi.fn(() => "blob:chapter-b-page");
    const revokeUrlMock = vi.fn();
    class TestURL extends URL {
      static createObjectURL = createUrlMock;
      static revokeObjectURL = revokeUrlMock;
    }
    vi.stubGlobal("URL", TestURL);
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === READER_SETTINGS_PATH) {
        return Promise.resolve(jsonResponse({ data: DEFAULT_READER_SETTINGS, meta: {} }));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 2, CHAPTER_ID));
      }
      if (url === `/api/v1/reader/${SECOND_CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 2, SECOND_CHAPTER_ID));
      }
      if (url.endsWith("/progress")) {
        const chapterId = url.includes(SECOND_CHAPTER_ID) ? SECOND_CHAPTER_ID : CHAPTER_ID;
        return Promise.resolve(progressResponse(null, "50.00", chapterId));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/pages/1`) {
        oldPageSignal = init?.signal ?? undefined;
        return new Promise<Response>((resolve) => { resolveOldPage = resolve; });
      }
      return Promise.resolve(pageResponse());
    });
    vi.stubGlobal("fetch", fetchMock);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0, gcTime: 0 } },
    });
    function RouteControls() {
      const navigate = useNavigate();
      return (
        <button onClick={() => navigate(`/reader/${SECOND_CHAPTER_ID}`)} type="button">
          Abrir capítulo siguiente
        </button>
      );
    }

    const view = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/reader/${CHAPTER_ID}`]}>
          <RouteControls />
          <Routes>
            <Route element={<ReaderPage />} path="/reader/:chapterId" />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByRole("heading", { level: 1, name: "Capítulo 8" });
    await waitFor(() => expect(oldPageSignal).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "Abrir capítulo siguiente" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Capítulo 9" }))
      .toBeInTheDocument();
    await screen.findByRole("img", { name: "Capítulo 9, página 1 de 2" });
    await waitFor(() => expect(oldPageSignal?.aborted).toBe(true));
    resolveOldPage?.(pageResponse());

    await waitFor(() => expect(createUrlMock).toHaveBeenCalledTimes(1));
    view.unmount();
    expect(createUrlMock).toHaveBeenCalledTimes(1);
    expect(revokeUrlMock).toHaveBeenCalledTimes(1);
    expect(revokeUrlMock).toHaveBeenCalledWith("blob:chapter-b-page");
  });

  it("does not refetch an evicted preload until it becomes current", async () => {
    let nextObjectUrl = 0;
    const createdUrls: string[] = [];
    const revokedUrls: string[] = [];
    let pageTwoRequests = 0;
    let pageThreeRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) {
        return Promise.resolve(chapterResponse(200, 4));
      }
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      if (url.endsWith("/pages/2")) pageTwoRequests += 1;
      if (url.endsWith("/pages/3")) pageThreeRequests += 1;
      return Promise.resolve(pageResponse());
    });
    const view = renderReaderPage(fetchMock, {
      ...DEFAULT_READER_SETTINGS,
      mode: "vertical",
      cache_limit_mb: 500,
    });
    const createObjectURL = vi.mocked(URL.createObjectURL);
    const revokeObjectURL = vi.mocked(URL.revokeObjectURL);
    createObjectURL.mockImplementation(() => {
      const url = `blob:live-cache-${nextObjectUrl}`;
      nextObjectUrl += 1;
      createdUrls.push(url);
      return url;
    });
    revokeObjectURL.mockImplementation((url) => { revokedUrls.push(url); });

    const pageTwo = await screen.findByRole("img", { name: "Capítulo 8, página 2 de 4" });
    await screen.findByRole("img", { name: "Capítulo 8, página 3 de 4" });
    await waitFor(() => expect(createdUrls).toHaveLength(3));
    const evictedUrl = pageTwo.getAttribute("src");
    expect(evictedUrl).not.toBeNull();
    Object.defineProperty(pageTwo, "naturalWidth", { configurable: true, value: 2_100 });
    Object.defineProperty(pageTwo, "naturalHeight", { configurable: true, value: 2_000 });
    fireEvent.load(pageTwo);

    view.queryClient.setQueryData(["reader-settings"], {
      ...DEFAULT_READER_SETTINGS,
      mode: "vertical",
      cache_limit_mb: 16,
    });
    await waitFor(() => expect(revokedUrls).toContain(evictedUrl));
    expect(pageTwoRequests).toBe(1);
    expect(revokedUrls.filter((url) => url === evictedUrl)).toHaveLength(1);

    await act(async () => {
      view.queryClient.setQueryData(["reader-settings"], {
        ...DEFAULT_READER_SETTINGS,
        mode: "vertical",
        cache_limit_mb: 16,
        preload_ahead: 1,
      });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await act(async () => {
      view.queryClient.setQueryData(["reader-settings"], {
        ...DEFAULT_READER_SETTINGS,
        mode: "vertical",
        cache_limit_mb: 16,
        preload_ahead: 2,
      });
    });
    await waitFor(() => expect(pageThreeRequests).toBe(2));
    expect(pageTwoRequests).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
    expect(await screen.findByRole("img", { name: "Capítulo 8, página 2 de 4" }))
      .toBeInTheDocument();
    await waitFor(() => expect(pageTwoRequests).toBe(2));

    view.unmount();
    expect(revokedUrls.filter((url) => url === evictedUrl)).toHaveLength(1);
    expect([...createdUrls].sort()).toEqual([...revokedUrls].sort());
  });

  it("does not write fallback preferences when loading saved settings fails", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) return Promise.resolve(chapterResponse());
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      if (url === READER_SETTINGS_PATH && init?.method === "PUT") {
        return Promise.resolve(jsonResponse({ data: DEFAULT_READER_SETTINGS, meta: {} }));
      }
      return Promise.resolve(pageResponse());
    });
    renderReaderPage(fetchMock, "error");

    expect(await screen.findByText(/No se pudieron cargar las preferencias del lector/))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Vertical" }));
    fireEvent.click(viewButton("Derecha a izquierda"));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(fetchMock.mock.calls.filter(([input, init]) => (
      String(input) === READER_SETTINGS_PATH && init?.method === "PUT"
    ))).toHaveLength(0);
  });

  it("shows a distinct not-found state when chapter metadata returns HTTP 404", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        error: {
          code: "READ_ASSET_NOT_FOUND",
          message: "El capítulo no está disponible.",
          retryable: false,
          trace_id: "trace-reader-chapter-404",
        },
      }, 404),
    );
    renderReaderPage(fetchMock, DEFAULT_READER_SETTINGS, { originatingSeriesId: "../downloads" });

    expect(await screen.findByRole("heading", { name: "Capítulo no disponible" }))
      .toBeInTheDocument();
    expect(screen.getByText("El capítulo no está disponible para lectura.")).toBeInTheDocument();
    expect(screen.getByText(/trace-reader-chapter-404/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "← Volver a descargas" })).toHaveAttribute(
      "href",
      "/downloads",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reports a page 404 and offers an explicit retry", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v1/reader/${CHAPTER_ID}`) return Promise.resolve(chapterResponse());
      if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
        return Promise.resolve(progressResponse(null));
      }
      return Promise.resolve(pageResponse(404));
    });
    renderReaderPage(fetchMock);

    expect(await screen.findByRole("heading", { name: "Página no encontrada" }))
      .toBeInTheDocument();
    expect(screen.getByText("No se encontró la página 1 de este capítulo.")).toBeInTheDocument();
    expect(screen.getByText(/trace-reader-page-not-found/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reintentar página" })).toBeEnabled();
  });

  it.each([100, 500, 2_000])(
    "bounds virtualized pages and live object URLs during a %i-page reader walk",
    async (pageCount) => {
      const createdUrls: string[] = [];
      const revokedUrls: string[] = [];
      const liveUrls = new Set<string>();
      const pageRequests: number[] = [];
      const byteLimit = 16_000_000;
      const estimatedPageBytes = 4 * 1024 * 1024 + 3;
      const maxRetainedPages = Math.floor(byteLimit / estimatedPageBytes);
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === `/api/v1/reader/${CHAPTER_ID}`) {
          return Promise.resolve(chapterResponse(200, pageCount));
        }
        if (url === `/api/v1/reader/${CHAPTER_ID}/progress`) {
          if (init?.method !== "PUT") return Promise.resolve(progressResponse(null));
          const pageIndex = (JSON.parse(String(init.body)) as { page_index: number }).page_index;
          return Promise.resolve(progressResponse(pageIndex));
        }
        if (url === READER_SETTINGS_PATH && init?.method === "PUT") {
          return Promise.resolve(jsonResponse({
            data: JSON.parse(String(init.body)) as ReaderSettings,
            meta: {},
          }));
        }
        const pageMatch = url.match(/\/pages\/(\d+)$/);
        const pageNumber = Number(pageMatch?.[1]);
        pageRequests.push(pageNumber);
        return Promise.resolve(pageResponse());
      });
      const view = renderReaderPage(fetchMock, {
        ...DEFAULT_READER_SETTINGS,
        cache_limit_mb: 16,
        preload_ahead: 0,
        mode: "vertical",
        e_reader: true,
      });
      vi.mocked(URL.createObjectURL).mockImplementation(() => {
        const url = `blob:k185-${createdUrls.length}`;
        createdUrls.push(url);
        liveUrls.add(url);
        return url;
      });
      vi.mocked(URL.revokeObjectURL).mockImplementation((url) => {
        revokedUrls.push(url);
        liveUrls.delete(url);
      });

      const pageRegion = await screen.findByRole("region", {
        name: "Páginas en lectura vertical",
      });
      for (let pageNumber = 1; pageNumber <= pageCount; pageNumber += 1) {
        const image = await screen.findByRole("img", {
          name: `Capítulo 8, página ${pageNumber} de ${pageCount}`,
        });
        Object.defineProperty(image, "naturalWidth", { configurable: true, value: 1024 });
        Object.defineProperty(image, "naturalHeight", { configurable: true, value: 1024 });
        fireEvent.load(image);

        expect(pageRegion.querySelectorAll("[data-testid='reader-page-slot']").length)
          .toBeLessThanOrEqual(4);
        expect(pageRegion.querySelectorAll("img").length).toBeLessThanOrEqual(4);
        expect(liveUrls.size).toBeLessThanOrEqual(maxRetainedPages);
        expect(liveUrls.size * estimatedPageBytes).toBeLessThanOrEqual(byteLimit);
        expect(liveUrls.size).toBe(createdUrls.length - revokedUrls.length);

        if (pageNumber < pageCount) {
          fireEvent.click(screen.getByRole("button", { name: "Página siguiente" }));
        }
      }

      expect(pageRequests).toEqual(Array.from({ length: pageCount }, (_, index) => index + 1));
      expect(new Set(revokedUrls).size).toBe(revokedUrls.length);
      view.unmount();
      expect(liveUrls.size).toBe(0);
      expect(createdUrls.slice().sort()).toEqual(revokedUrls.slice().sort());
    },
    180_000,
  );
});
