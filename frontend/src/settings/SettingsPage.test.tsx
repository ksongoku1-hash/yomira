import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsPage } from "./SettingsPage";
import { useAppearanceStore } from "../app/appearanceStore";

const connection = vi.hoisted(() => ({ mobile: false }));
vi.mock("./MobileAccessPanel", () => ({ MobileAccessPanel: () => null, get isMobileConnection() { return connection.mobile; } }));
vi.mock("./ReaderCachePanel", () => ({ ReaderCachePanel: () => null }));
vi.mock("../app/AppUpdates", () => ({ AppUpdates: () => null }));

function mockResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => payload,
  } as unknown as Response;
}

function renderSettingsPage() {
  const readerFetch = globalThis.fetch;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
    String(input) === "/api/v1/library/local-imports"
      ? Promise.resolve(mockResponse({ folder: "C:/Mangas de Yomira", scanning: false,
          imported_chapters: 0, pending_metadata: 0, errors: [] }))
      : readerFetch(input, init),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SettingsPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  connection.mobile = false;
  vi.unstubAllGlobals();
});

describe("SettingsPage", () => {
  it("keeps PC folder actions out of mobile connection settings", async () => {
    connection.mobile = true;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse({ data: {
      cache_limit_mb: 500, preload_ahead: 2, mode: "single", rtl: false, e_reader: false,
    }, meta: {} })));
    renderSettingsPage();
    expect(await screen.findByRole("heading", { name: "Ajustes" })).toBeInTheDocument();
    await screen.findByRole("spinbutton", { name: /presupuesto de caché del lector/i });
    expect(screen.queryByRole("heading", { name: "Carpetas de tus mangas" })).not.toBeInTheDocument();
    expect(screen.getByText("Personaliza la apariencia y ajusta el lector.")).toBeInTheDocument();
  });
  it("applies appearance immediately without submitting reader settings", async () => {
    useAppearanceStore.setState({ enabled: true, intensity: 60, simpleGraphics: false, theme: "tinta", storageError: false });
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValue(mockResponse(null, 500));
    vi.stubGlobal("fetch", fetchMock);
    renderSettingsPage();
    fireEvent.change(screen.getByRole("combobox", { name: "Tema de la interfaz" }), { target: { value: "papel" } });
    expect(useAppearanceStore.getState().theme).toBe("papel");
    expect(screen.getByRole("combobox", { name: "Tema de la interfaz" })).toHaveValue("papel");
    const slider = screen.getByRole("slider", { name: "Intensidad del fondo: 60%" });
    fireEvent.change(slider, { target: { value: "35" } });
    expect(useAppearanceStore.getState().intensity).toBe(35);
    expect(screen.getByRole("slider", { name: "Intensidad del fondo: 35%" })).toHaveValue("35");
    fireEvent.click(screen.getByRole("checkbox", { name: "Fondos ilustrados" }));
    expect(slider).toBeDisabled();
    expect(useAppearanceStore.getState().enabled).toBe(false);
    fireEvent.click(screen.getByRole("checkbox", { name: "Gráficos sencillos" }));
    expect(useAppearanceStore.getState().simpleGraphics).toBe(true);
    await screen.findByRole("alert");
    expect(fetchMock.mock.calls.every((call) => call[1]?.method !== "PUT")).toBe(true);
    useAppearanceStore.setState({ enabled: true, intensity: 60, simpleGraphics: false, theme: "tinta", storageError: false });
  });
  it("edits only cache and bounded preload settings while preserving the full DTO", async () => {
    const settings = {
      cache_limit_mb: 256,
      preload_ahead: 1,
      mode: "double",
      rtl: true,
      e_reader: true,
    };
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        return Promise.resolve(mockResponse({ data: JSON.parse(String(init.body)), meta: {} }));
      }
      return Promise.resolve(mockResponse({ data: settings, meta: {} }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSettingsPage();

    const cacheLimit = await screen.findByRole("spinbutton", {
      name: /presupuesto de caché del lector/i,
    });
    const preloadAhead = screen.getByRole("combobox", {
      name: /pre-carga de páginas siguientes/i,
    });
    expect(cacheLimit).toHaveAttribute("min", "16");
    expect(cacheLimit).toHaveAttribute("max", "500");
    expect(preloadAhead).toHaveValue("1");
    expect(screen.queryByLabelText(/modo de lectura|dirección de lectura|rtl/i))
      .not.toBeInTheDocument();

    fireEvent.change(cacheLimit, { target: { value: "320" } });
    fireEvent.change(preloadAhead, { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar ajustes" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/settings/reader", {
      method: "PUT",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        cache_limit_mb: 320,
        preload_ahead: 2,
        mode: "double",
        rtl: true,
        e_reader: true,
      }),
    }));
    expect(await screen.findByText(/^Ajustes guardados\.$/)).toHaveAttribute("role", "status");
  });

  it("shows traced load failures and retries only when requested", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(mockResponse({
        error: {
          code: "API_VALIDATION_FAILED",
          message: "No se pudieron cargar los ajustes.",
          retryable: false,
          trace_id: "trace-reader-settings",
        },
      }, 500))
      .mockResolvedValueOnce(mockResponse({
        data: {
          cache_limit_mb: 128,
          preload_ahead: 0,
          mode: "single",
          rtl: false,
          e_reader: false,
        },
        meta: {},
      }));
    vi.stubGlobal("fetch", fetchMock);
    renderSettingsPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("API_VALIDATION_FAILED");
    expect(alert).toHaveTextContent("trace-reader-settings");
    fireEvent.click(screen.getByRole("button", { name: "Reintentar carga" }));

    expect(await screen.findByRole("spinbutton", {
      name: /presupuesto de caché del lector/i,
    })).toHaveValue(128);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the draft after a traced save failure and retries on explicit action", async () => {
    const initialSettings = {
      cache_limit_mb: 128,
      preload_ahead: 0,
      mode: "single",
      rtl: false,
      e_reader: false,
    };
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== "PUT") {
        return Promise.resolve(mockResponse({ data: initialSettings, meta: {} }));
      }
      if (fetchMock.mock.calls.filter((call) => call[1]?.method === "PUT").length === 1) {
        return Promise.resolve(mockResponse({
          error: {
            code: "API_VALIDATION_FAILED",
            message: "No se pudieron guardar los ajustes.",
            retryable: false,
            trace_id: "trace-reader-settings-put",
          },
        }, 500));
      }
      return Promise.resolve(mockResponse({ data: JSON.parse(String(init.body)), meta: {} }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSettingsPage();

    const cacheLimit = await screen.findByRole("spinbutton", {
      name: /presupuesto de caché del lector/i,
    });
    fireEvent.change(cacheLimit, { target: { value: "256" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar ajustes" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("API_VALIDATION_FAILED");
    expect(alert).toHaveTextContent("trace-reader-settings-put");
    expect(cacheLimit).toHaveValue(256);
    fireEvent.click(screen.getByRole("button", { name: "Guardar ajustes" }));

    expect(await screen.findByText(/^Ajustes guardados\.$/)).toHaveAttribute("role", "status");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
