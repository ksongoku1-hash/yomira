import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DownloadsPage } from "./DownloadsPage";

function mockResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => payload,
  } as unknown as Response;
}

function job(index: number, status = "QUEUED") {
  const suffix = index.toString(16).padStart(12, "0");
  return {
    job_id: `123e4567-e89b-42d3-a456-${suffix}`,
    chapter_source_id: `123e4567-e89b-42d3-a456-${(index + 1000).toString(16).padStart(12, "0")}`,
    series_id: "123e4567-e89b-42d3-a456-426614174050",
    chapter_id: `123e4567-e89b-42d3-a456-${(index + 2000).toString(16).padStart(12, "0")}`,
    status,
    priority: "NORMAL",
    series_title: "Mock Fixture Series",
    chapter_label: `Capítulo ${index}`,
    created_at: "2026-09-27T10:00:00Z",
    updated_at: "2026-09-27T10:05:00Z",
    cancel_requested_at: null,
  };
}

function renderDownloadsPage(withGroups = false) {
  if (!withGroups) {
    const fetchJobs = globalThis.fetch;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).includes("/download-jobs/groups?")
        ? Promise.resolve(mockResponse({data: [], meta: {limit: 50, offset: 0, total: 0}}))
        : fetchJobs(input, init));
  }
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/downloads"]}>
        <Routes>
          <Route element={<DownloadsPage />} path="/downloads" />
          <Route element={<p>Ruta del lector</p>} path="/reader/:chapterId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("DownloadsPage", () => {
  it("pauses and resumes an entire manga group even across job pages", async () => {
    const seriesId = job(1).series_id;
    let paused = false;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/pause")) {
        paused = JSON.parse(String(init?.body)).paused;
        return Promise.resolve(mockResponse(null, 204));
      }
      if (path.includes("/download-jobs/groups?")) return Promise.resolve(mockResponse({
        data: [{series_id: seriesId, series_title: "Grupo de prueba", queued: 75, active: 1, paused}],
        meta: {limit: 50, offset: 0, total: 1},
      }));
      return Promise.resolve(mockResponse({data: [job(1)], meta: {limit: 50, offset: 0, total: 1}}));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage(true);
    fireEvent.click(await screen.findByRole("button", {name: "Pausar grupo de Grupo de prueba"}));
    expect(await screen.findByText("Pausa solicitada; termina el capítulo en curso.")).toBeInTheDocument();
    expect(screen.getByText("75 pendientes · 1 en curso")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", {name: "Reanudar grupo de Grupo de prueba"}));
    expect(await screen.findByRole("button", {name: "Pausar grupo de Grupo de prueba"})).toBeEnabled();
    expect(fetchMock.mock.calls.filter(([path]) => String(path).endsWith("/pause")).map(([, init]) =>
      [init?.method, JSON.parse(String(init?.body))])).toEqual([["PUT", {paused: true}], ["PUT", {paused: false}]]);
  });

  it("keeps the group active when the server rejects the pause", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/pause")) return Promise.resolve(mockResponse({error: {
        code: "API_NOT_READY", message: "No disponible", trace_id: "pause-test", retryable: true,
      }}, 503));
      if (path.includes("/groups?")) return Promise.resolve(mockResponse({
        data: [{series_id: job(1).series_id, series_title: "Grupo de prueba", queued: 2, active: 0, paused: false}],
        meta: {limit: 50, offset: 0, total: 1},
      }));
      return Promise.resolve(mockResponse({data: [job(1)], meta: {limit: 50, offset: 0, total: 1}}));
    }));
    renderDownloadsPage(true);
    fireEvent.click(await screen.findByRole("button", {name: "Pausar grupo de Grupo de prueba"}));
    expect(await screen.findByText("No se pudo cambiar la pausa del grupo")).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /Reanudar grupo/})).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Pausar grupo de Grupo de prueba"})).toBeEnabled();
  });

  it("explains an automatic alternate-source search while preserving the failed chapter", async () => {
    const failed = job(1, "FAILED");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse({
      data: [failed], meta: {
        limit: 50, offset: 0, total: 1, recovering_job_id: failed.job_id,
      },
    })));
    renderDownloadsPage();
    expect(await screen.findByText(/Yomira está buscando el mismo capítulo en otras fuentes/)).toBeInTheDocument();
    expect(screen.getByText("Capítulo 1")).toBeInTheDocument();
  });
  it("shows totals across pagination and measured page progress while a source waits", async () => {
    const active = job(1, "DOWNLOADING");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse({
      data: [active], meta: {
        limit: 50, offset: 0, total: 72,
        states: { QUEUED: 60, DOWNLOADING: 1, COMPLETED: 10, FAILED: 1 },
        page_progress: [{ job_id: active.job_id, known_pages: 3, validated_pages: 1 }],
        waiting_sources: 1, retry_after_seconds: 30,
      },
    })));
    renderDownloadsPage();
    expect(await screen.findByText("60 pendientes · 1 en curso")).toBeInTheDocument();
    expect(screen.getByText("1 de 3 páginas comprobadas")).toBeInTheDocument();
    const progress = screen.getByRole("progressbar");
    expect(progress).toHaveAttribute("value", "1");
    expect(progress).toHaveAttribute("max", "3");
    expect(screen.getByText(/Sus capítulos pendientes se conservan/)).toBeInTheDocument();
    expect(screen.getByText("Terminadas").nextElementSibling).toHaveTextContent("10");
  });
  it("lets a fully cancelled terminal job leave the visible queue", async () => {
    const cancelled = {
      ...job(1, "CANCELLED"),
      cancel_requested_at: "2026-09-27T10:06:00Z",
    };
    let archived = false;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/archive")) {
        archived = true;
        return Promise.resolve(mockResponse(null, 204));
      }
      const archivedView = new URL(path, "http://localhost").searchParams.get("archived") === "true";
      const data = archivedView
        ? archived ? [cancelled] : []
        : archived ? [] : [cancelled];
      return Promise.resolve(mockResponse({
        data,
        meta: { limit: 50, offset: 0, total: data.length },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    const removeButton = await screen.findByRole("button", {
      name: "Quitar Capítulo 1 de Mock Fixture Series de la cola",
    });
    fireEvent.click(removeButton);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/download-jobs/${cancelled.job_id}/archive`,
      { headers: { Accept: "application/json" }, method: "POST" },
    ));
  });

  it("removes terminal jobs from the queue and allows restoring them from the archived view", async () => {
    const failed = job(1, "FAILED");
    const active = job(2, "DOWNLOADING");
    let isRemoved = false;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/archive")) {
        isRemoved = true;
        return Promise.resolve(mockResponse(null, 204));
      }
      if (path.endsWith("/restore")) {
        isRemoved = false;
        return Promise.resolve(mockResponse(null, 204));
      }
      const archivedView = new URL(path, "http://localhost").searchParams.get("archived") === "true";
      const data = archivedView
        ? isRemoved ? [failed] : []
        : isRemoved ? [active] : [failed, active];
      return Promise.resolve(mockResponse({
        data,
        meta: { limit: 50, offset: 0, total: data.length },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    expect(await screen.findByText("Descargando")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Quitar Capítulo 1 de Mock Fixture Series de la cola" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Quitar Capítulo 2/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Quitar Capítulo 1 de Mock Fixture Series de la cola" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/download-jobs/${failed.job_id}/archive`,
      { headers: { Accept: "application/json" }, method: "POST" },
    ));

    fireEvent.click(await screen.findByRole("button", { name: "Ver trabajos retirados" }));
    expect(await screen.findByText("Fallido")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restaurar Capítulo 1 de Mock Fixture Series" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/download-jobs/${failed.job_id}/restore`,
      { headers: { Accept: "application/json" }, method: "POST" },
    ));
  });

  it("retries a failed chapter explicitly and refreshes without replacing its history", async () => {
    const failed = job(1, "FAILED");
    const retried = { ...failed, job_id: job(2).job_id, status: "QUEUED" };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(mockResponse({ data: [failed], meta: { limit: 50, offset: 0, total: 1 } }))
      .mockResolvedValueOnce(mockResponse({ data: retried }))
      .mockResolvedValue(mockResponse({ data: [retried, failed], meta: { limit: 50, offset: 0, total: 2 } }));
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    fireEvent.click(await screen.findByRole("button", { name: "Reintentar Capítulo 1 de Mock Fixture Series" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/downloads/${failed.job_id}/retry`,
      { headers: { Accept: "application/json" }, method: "POST" },
    ));
    expect(await screen.findByText("En cola")).toBeInTheDocument();
    expect(screen.getByText("Fallido")).toBeInTheDocument();
    expect(screen.getByText(retried.job_id)).toBeInTheDocument();
    expect(screen.getByText(failed.job_id)).toBeInTheDocument();
    expect(screen.getByText("Se creó un nuevo intento; este fallo se conserva en el historial."))
      .toHaveAttribute("role", "status");
  });

  it("offers retry for failed and interrupted jobs", async () => {
    const statuses = ["FAILED", "CANCELLED", "INTERRUPTED", "COMPLETED", "QUEUED", "DOWNLOADING"];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse({
      data: statuses.map((status, index) => job(index + 1, status)),
      meta: { limit: 50, offset: 0, total: statuses.length },
    })));
    renderDownloadsPage();

    expect(await screen.findByRole("button", { name: /Reintentar Capítulo 1/ })).toBeEnabled();
    expect(screen.getAllByRole("button", { name: /Reintentar Capítulo/ })).toHaveLength(2);
  });

  it("disables retry while its request is pending", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(mockResponse({ data: [job(1, "FAILED")], meta: { limit: 50, offset: 0, total: 1 } }))
      .mockImplementationOnce(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    const retry = await screen.findByRole("button", { name: /Reintentar Capítulo 1/ });
    fireEvent.click(retry);
    await waitFor(() => expect(retry).toBeDisabled());
    fireEvent.click(retry);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps a rejected retry visible with its code and trace", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(mockResponse({ data: [job(1, "FAILED")], meta: { limit: 50, offset: 0, total: 1 } }))
      .mockResolvedValueOnce(mockResponse({ error: {
        code: "SRC_CONNECTOR_DISABLED", message: "El conector está deshabilitado.",
        retryable: false, trace_id: "trace-retry-job",
      } }, 409)));
    renderDownloadsPage();

    fireEvent.click(await screen.findByRole("button", { name: /Reintentar Capítulo 1/ }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("SRC_CONNECTOR_DISABLED");
    expect(alert).toHaveTextContent("trace-retry-job");
    expect(screen.getByRole("button", { name: /Reintentar Capítulo 1/ })).toBeEnabled();
    expect(screen.getByText("Fallido")).toBeInTheDocument();
  });

  it("shows the queue count and keeps progress details available without inventing percentages", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({ data: [job(1)], meta: { limit: 50, offset: 0, total: 1 } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    expect(await screen.findByRole("heading", { level: 3, name: "Mock Fixture Series" }))
      .toBeInTheDocument();
    expect(screen.getByText("1 trabajo guardado")).toHaveAttribute("role", "status");
    expect(screen.getByText("Capítulo 1")).toBeInTheDocument();
    expect(screen.getByText("En cola")).toBeInTheDocument();
    expect(screen.getByText("Cómo se muestra el progreso y el historial")).toBeInTheDocument();
    expect(screen.getByText(/no se muestra un porcentaje estimado/i)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/download-jobs?limit=50&offset=0", {
      headers: { Accept: "application/json" },
    });
  });

  it("shows active durable stages without inventing page progress or percentages", async () => {
    const activeStatuses = [
      "FETCHING_METADATA",
      "DOWNLOADING",
      "VALIDATING",
      "PROCESSING",
      "PACKAGING",
      "VERIFYING",
      "INDEXING",
    ];
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({
        data: activeStatuses.map((status, index) => job(index + 1, status)),
        meta: { limit: 50, offset: 0, total: activeStatuses.length },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    for (const label of [
      "Preparando metadatos",
      "Descargando",
      "Validando",
      "Procesando",
      "Preparando archivo",
      "Verificando",
      "Añadiendo a biblioteca",
    ]) {
      expect(await screen.findByText(label)).toBeInTheDocument();
    }
    expect(screen.getAllByRole("heading", { level: 3, name: "Mock Fixture Series" }))
      .toHaveLength(activeStatuses.length);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.queryByText(/\b\d+%/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Descargado/i)).not.toBeInTheDocument();
  });

  it("offers cancellation only while a job is in a cancellable stage", async () => {
    const cancellableStatuses = [
      "QUEUED",
      "FETCHING_METADATA",
      "DOWNLOADING",
      "VALIDATING",
      "PROCESSING",
      "PACKAGING",
      "VERIFYING",
      "INDEXING",
    ];
    const terminalOrBlockedStatuses = ["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"];
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({
        data: [
          ...cancellableStatuses.map((status, index) => job(index + 1, status)),
          ...terminalOrBlockedStatuses.map((status, index) =>
            job(index + cancellableStatuses.length + 1, status),
          ),
        ],
        meta: {
          limit: 50,
          offset: 0,
          total: cancellableStatuses.length + terminalOrBlockedStatuses.length,
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    expect(await screen.findByText("Capítulo 1")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /cancelar/i })).toHaveLength(
      cancellableStatuses.length,
    );
    expect(
      screen.queryByRole("button", { name: /cancelar.*Capítulo 9/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /cancelar.*Capítulo 10/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /cancelar.*Capítulo 11/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /cancelar.*Capítulo 12/i }),
    ).not.toBeInTheDocument();
  });

  it("shows a durable pending cancellation and removes its cancel control", async () => {
    const pendingAt = "2026-09-27T10:06:00Z";
    const pendingJob = { ...job(1, "DOWNLOADING"), cancel_requested_at: pendingAt };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        mockResponse({ data: [job(1, "DOWNLOADING")], meta: { limit: 50, offset: 0, total: 1 } }),
      )
      .mockResolvedValueOnce(mockResponse({ data: pendingJob }));
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    fireEvent.click(await screen.findByRole("button", { name: "Cancelar Capítulo 1 de Mock Fixture Series" }));

    expect(await screen.findByRole("status", { name: "Cancelación solicitada" }))
      .toHaveTextContent("se completará en un punto seguro");
    expect(
      screen.queryByRole("button", { name: "Cancelar Capítulo 1 de Mock Fixture Series" }),
    ).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/downloads/123e4567-e89b-42d3-a456-000000000001/cancel",
      { headers: { Accept: "application/json" }, method: "POST" },
    );
  });

  it.each([
    ["FAILED", "Fallido", "falló antes de alcanzar un punto seguro"],
    ["INTERRUPTED", "Interrumpido", "permanece pendiente de recuperación"],
  ] as const)(
    "keeps the durable intent visible when cancellation races with %s",
    async (status, label, message) => {
      const cancelledJob = {
        ...job(1, "DOWNLOADING"),
        status,
        cancel_requested_at: "2026-09-27T10:06:00Z",
      };
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(
          mockResponse({ data: [job(1, "DOWNLOADING")], meta: { limit: 50, offset: 0, total: 1 } }),
        )
        .mockResolvedValueOnce(mockResponse({ data: cancelledJob }));
      vi.stubGlobal("fetch", fetchMock);
      renderDownloadsPage();

      fireEvent.click(await screen.findByRole("button", { name: "Cancelar Capítulo 1 de Mock Fixture Series" }));

      expect(await screen.findByText(label)).toBeInTheDocument();
      expect(await screen.findByRole("status", { name: "Cancelación solicitada" }))
        .toHaveTextContent(message);
      expect(screen.queryByRole("button", { name: /cancelar/i })).not.toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );

  it("restores pending cancellation from the durable jobs list", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({
        data: [{ ...job(1, "PACKAGING"), cancel_requested_at: "2026-09-27T10:06:00Z" }],
        meta: { limit: 50, offset: 0, total: 1 },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    expect(await screen.findByRole("status", { name: "Cancelación solicitada" }))
      .toHaveTextContent("se completará en un punto seguro");
    expect(screen.queryByRole("button", { name: /cancelar/i })).not.toBeInTheDocument();
  });

  it("shows terminal cancellation immediately when the server confirms it", async () => {
    const cancelledJob = { ...job(1, "CANCELLED"), cancel_requested_at: "2026-09-27T10:06:00Z" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        mockResponse({ data: [job(1)], meta: { limit: 50, offset: 0, total: 1 } }),
      )
      .mockResolvedValueOnce(mockResponse({ data: cancelledJob }));
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    fireEvent.click(await screen.findByRole("button", { name: "Cancelar Capítulo 1 de Mock Fixture Series" }));

    expect(await screen.findByText("Cancelado")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Cancelación solicitada" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cancelar/i })).not.toBeInTheDocument();
  });

  it("shows only the sanitized server error code and trace when cancellation is rejected", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        mockResponse({ data: [job(1)], meta: { limit: 50, offset: 0, total: 1 } }),
      )
      .mockResolvedValueOnce(
        mockResponse(
          {
            error: {
              code: "DB_CONNECTION_FAILED",
              message: "No se pudo guardar la solicitud de cancelación.",
              retryable: true,
              trace_id: "trace-cancel-job",
            },
          },
          503,
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    fireEvent.click(await screen.findByRole("button", { name: "Cancelar Capítulo 1 de Mock Fixture Series" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("DB_CONNECTION_FAILED");
    expect(alert).toHaveTextContent("trace-cancel-job");
    expect(alert).toHaveTextContent("HTTP 503");
    expect(screen.getByRole("button", { name: "Cancelar Capítulo 1 de Mock Fixture Series" }))
      .toBeEnabled();
  });

  it("shows FAILED and COMPLETED as distinct terminal states", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({
        data: [job(1, "FAILED"), job(2, "COMPLETED")],
        meta: { limit: 50, offset: 0, total: 2 },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    expect(await screen.findByText("Fallido")).toBeInTheDocument();
    expect(screen.getByText("Completado")).toBeInTheDocument();
    expect(screen.getByText("Capítulo 1")).toBeInTheDocument();
    expect(screen.getByText("Capítulo 2")).toBeInTheDocument();
  });

  it("offers a reader link only for a COMPLETED job with its chapter ID", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({
        data: [job(1, "COMPLETED"), job(2, "FAILED")],
        meta: { limit: 50, offset: 0, total: 2 },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    expect(await screen.findByRole("link", { name: "Leer Capítulo 1 de Mock Fixture Series" }))
      .toHaveAttribute("href", "/reader/123e4567-e89b-42d3-a456-0000000007d1");
    expect(screen.queryByRole("link", { name: "Leer Capítulo 2 de Mock Fixture Series" }))
      .not.toBeInTheDocument();
  });

  it("retains the server code and trace and retries only after an explicit action", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        mockResponse(
          {
            error: {
              code: "DB_CONNECTION_FAILED",
              message: "No se pudo leer la cola local.",
              retryable: true,
              trace_id: "trace-jobs-list",
            },
          },
          503,
        ),
      )
      .mockResolvedValueOnce(
        mockResponse({ data: [job(1)], meta: { limit: 50, offset: 0, total: 1 } }),
      );
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("DB_CONNECTION_FAILED");
    expect(alert).toHaveTextContent("trace-jobs-list");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Reintentar carga" }));
    expect(await screen.findByText("Capítulo 1")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("paginates in bounded pages of fifty jobs", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("offset=0")) {
        return Promise.resolve(
          mockResponse({ data: [job(1)], meta: { limit: 50, offset: 0, total: 51 } }),
        );
      }
      return Promise.resolve(
        mockResponse({ data: [job(51)], meta: { limit: 50, offset: 50, total: 51 } }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDownloadsPage();

    expect(await screen.findByText("Capítulo 1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Siguiente" }));

    expect(await screen.findByText("Capítulo 51")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenLastCalledWith("/api/v1/download-jobs?limit=50&offset=50", {
      headers: { Accept: "application/json" },
    });
    expect(screen.getByText("Página 2 de 2")).toBeInTheDocument();
  });
});
