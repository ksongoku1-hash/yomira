import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MetadataField, MetadataValue } from "../api/client";
import { SeriesDetailPage } from "./SeriesDetailPage";

const seriesId = "123e4567-e89b-42d3-a456-426614174000";
const coverAssetId = "123e4567-e89b-42d3-a456-426614174001";
const secondSeriesId = "123e4567-e89b-42d3-a456-426614174002";
const cbrChapterId = "123e4567-e89b-42d3-a456-426614174006";

function seriesRecord(
  updatedAt = "2026-09-30T10:00:00Z",
  sourceDisplayName: string | null = null,
) {
  const values: Record<MetadataField, MetadataValue> = {
    title: "Mi título local",
    cover: coverAssetId,
    author: "Autor elegido por ti",
    description: "Sinopsis guardada con saltos de línea.\nSegundo párrafo.",
    genres: ["Fantasía", "Acción"],
    status: "ongoing",
    language: "es",
  };
  return {
    series_id: seriesId,
    canonical_title: "Título canónico",
    display_title: values.title,
    author: values.author,
    genres: values.genres,
    description: values.description,
    status: values.status,
    language: values.language,
    cover_asset_id: coverAssetId,
    created_at: "2026-09-30T10:00:00Z",
    updated_at: updatedAt,
    metadata: (Object.keys(values) as MetadataField[]).map((field) => ({
      field,
      value: values[field],
      authority: field === "title" || field === "author" ? "USER_OVERRIDE" : "LOCAL_CANONICAL",
      local_value: values[field],
      source_values: field === "title" && sourceDisplayName !== null
        ? [{
            series_source_id: "123e4567-e89b-42d3-a456-426614174003",
            source_id: "123e4567-e89b-42d3-a456-426614174004",
            source_display_name: sourceDisplayName,
            value: values[field],
          }]
        : [] as {
            series_source_id: string; source_id: string;
            source_display_name: string | null; value: MetadataValue;
          }[],
      has_user_override: field === "title" || field === "author",
      user_override_value: field === "title" || field === "author" ? values[field] : null,
      source_conflict: false,
      effective_source_ids: [] as string[],
    })),
  };
}

function response(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

function renderDetail(client: QueryClient) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/series/${seriesId}`]}>
        <Link to={`/series/${secondSeriesId}`}>Abrir segunda serie</Link>
        <Routes><Route element={<SeriesDetailPage />} path="/series/:seriesId" /><Route element={<ReaderDestination />} path="/reader/:chapterId" /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function ReaderDestination() {
  const location = useLocation();
  return <p>{location.pathname}{location.search}</p>;
}

it.each([false, true])("continues from persisted progress and chooses the available copy (local=%s)", async (local) => {
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const path = String(input);
    if (path === `/api/v1/series/${seriesId}`) return Promise.resolve(response({data: seriesRecord(), meta: {}}));
    if (path === "/api/v1/reader/progress/recent?limit=20") return Promise.resolve(response({
      data: [{series_id: seriesId, display_title: "Mi título local", chapter_id: cbrChapterId,
        display_label: "Capítulo 8", page_index: 9, percentage: "50.00", updated_at: "2026-10-12T00:00:00Z"}], meta: {limit: 20},
    }));
    if (path.endsWith("/download-source")) return Promise.resolve(response({data: {
      chapter_id: cbrChapterId, connector_id: "yomira.mock", remote_series_id: "fixture", chapter_remote_id: "chapter-8",
    }, meta: {}}));
    if (path.endsWith("/availability")) return Promise.resolve(response({data: {
      linked: true, ready_remote_ids: local ? ["chapter-8"] : [], ready_labels: [], busy_remote_ids: [],
    }}));
    return Promise.resolve(response({data: [], meta: {limit: 100, offset: 0, total: 0}}));
  }));
  renderDetail(new QueryClient({defaultOptions: {queries: {retry: false, gcTime: 0}}}));
  fireEvent.click(await screen.findByRole("link", {name: "Continuar Capítulo 8 · Página 10"}));
  expect(await screen.findByText(`/reader/${cbrChapterId}${local ? "" : "?online=1"}`)).toBeInTheDocument();
});

function refreshButton() {
  const menu = screen.getByLabelText("Más acciones del manga");
  if (!menu.closest("details")?.open) fireEvent.click(menu);
  return screen.getByRole("button", { name: "Actualizar metadatos y portada" });
}

function metadataButton() {
  if (!screen.queryByRole("button", { name: "Buscar datos externos" })) fireEvent.click(screen.getByText("Metadatos y procedencia"));
  return screen.getByRole("button", { name: "Buscar datos externos" });
}

afterEach(() => vi.unstubAllGlobals());

it("shows source metadata, cover and online chapters for a series with no downloaded chapters", async () => {
  const base = seriesRecord();
  const record = {...base, cover_asset_id: null, description: null,
    metadata: base.metadata.map((item) => ["cover", "description"].includes(item.field)
      ? {...item, value: null, local_value: null, authority: null, source_conflict: true} : item),
    reading_sources: [{series_source_id: "123e4567-e89b-42d3-a456-426614174003",
      connector_id: "yomira.mock", remote_series_id: "fixture", display_name: "Fuente elegida",
      author: "Autor de la fuente", description: "Sinopsis de la fuente elegida",
      genres: ["Deporte"], status: "completed", language: "es"}],
  };
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const path = String(input);
    if (path === `/api/v1/series/${seriesId}`) return Promise.resolve(response({data: record, meta: {}}));
    if (path.endsWith("/chapters") && path.includes("/connectors/")) return Promise.resolve(response({
      data: [{remote_id: "chapter-1", remote_url: null, remote_label: "1", chapter_raw_label: "1",
        chapter_sort_key: [1], language: "es", display_label: "Capítulo 1"}],
      meta: {connector_id: "yomira.mock", remote_series_id: "fixture"},
    }));
    if (path.endsWith("/cover")) return Promise.resolve(response({data: {cover_url: "https://example.test/cover.jpg"},
      meta: {connector_id: "yomira.mock", remote_series_id: "fixture"}}));
    return Promise.resolve(response({data: [], meta: {limit: 100, offset: 0, total: 0}}));
  });
  vi.stubGlobal("fetch", fetchMock);
  renderDetail(new QueryClient({defaultOptions: {queries: {retry: false}}}));
  expect(await screen.findByRole("button", {name: "Leer online Capítulo 1"})).toBeInTheDocument();
  expect(screen.getByText("Sinopsis de la fuente elegida")).toBeInTheDocument();
  expect(await screen.findByRole("img", {name: "Portada de Mi título local"})).toHaveAttribute("src", "https://example.test/cover.jpg");
  expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/download-jobs"))).toBe(false);
  expect(screen.getByText(/todavía no tiene capítulos descargados/)).toBeInTheDocument();
});

describe("series metadata refresh", () => {
  it("loads chapters in descending order and restores ascending order", async () => {
    let finishDescending: (() => void) | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/v1/series/${seriesId}`) return Promise.resolve(response({data: seriesRecord(), meta: {}}));
      if (path.includes(`/reader/series/${seriesId}/chapters?`)) {
        const numbers = path.includes("order=desc") ? [10, 1] : [1, 10];
        const reply = response({data: numbers.map((number) => ({
          chapter_id: `123e4567-e89b-42d3-a456-${number.toString().padStart(12, "0")}`,
          series_id: seriesId, display_label: `Capítulo ${number}`, page_count: 2,
        })), meta: {limit: 100, offset: 0, total: 2}});
        return path.includes("order=desc")
          ? new Promise<Response>((resolve) => { finishDescending = () => resolve(reply); })
          : Promise.resolve(reply);
      }
      return Promise.resolve(response({data: [], meta: {limit: 20}}));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDetail(new QueryClient({defaultOptions: {queries: {retry: false}}}));
    await screen.findByRole("list", {name: "Capítulos disponibles para lectura"});
    const labels = () => within(screen.getByRole("list", {name: "Capítulos disponibles para lectura"}))
      .getAllByRole("link").map((link) => link.textContent);
    expect(labels()[0]).toContain("Capítulo 1");
    const orderButton = screen.getByRole("button", {name: "Cambiar orden de capítulos para leer"});
    fireEvent.click(orderButton);
    await waitFor(() => expect(orderButton).toBeDisabled());
    expect(orderButton).toBeInTheDocument();
    expect(orderButton).toHaveAttribute("aria-pressed", "true");
    expect(labels()[0]).toContain("Capítulo 1");
    finishDescending!();
    await waitFor(() => expect(labels()[0]).toContain("Capítulo 10"));
    expect(await screen.findByRole("link", {name: /Leer capítulo más reciente/})).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Cambiar orden de capítulos para leer"}));
    await waitFor(() => expect(labels()[0]).toContain("Capítulo 1"));
    expect(fetchMock.mock.calls.some(([path]) => String(path).includes("offset=0&order=desc"))).toBe(true);
  });
  it("reports metadata still missing after a successful refresh", async () => {
    const base = seriesRecord();
    const record = {
      ...base, author: null,
      metadata: base.metadata.map((item) => item.field === "author" ? {
        ...item, value: null, local_value: null, has_user_override: false,
        authority: "LOCAL_CANONICAL", user_override_value: null,
      } : item),
    };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/v1/series/${seriesId}` || path.endsWith("/metadata/refresh")) {
        return Promise.resolve(response({ data: record, meta: {} }));
      }
      return Promise.resolve(response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    }));
    renderDetail(new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    const cover = await screen.findByRole("img", { name: "Portada de Mi título local" });
    expect(document.querySelector('div[aria-hidden="true"] > img'))
      .toHaveAttribute("src", cover.getAttribute("src"));
    fireEvent.click(refreshButton());
    expect(await screen.findByText(/Búsqueda terminada. Quedan datos pendientes: Autor/))
      .toBeInTheDocument();
    expect(screen.queryByText("Metadatos y portada actualizados.")).not.toBeInTheDocument();
  });
  it("builds an aggregate CBR from selected ready chapters", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/series/" + seriesId) {
        return Promise.resolve(response({ data: seriesRecord(), meta: {} }));
      }
      if (path.startsWith("/api/v1/reader/series/" + seriesId + "/chapters?")) {
        return Promise.resolve(response({
          data: [{
            chapter_id: cbrChapterId,
            series_id: seriesId,
            display_label: "Capítulo 2",
            page_count: 2,
          }],
          meta: { limit: 100, offset: 0, total: 1 },
        }));
      }
      if (path === "/api/v1/cbr-exports") {
        expect(JSON.parse(String(init?.body))).toEqual({
          series_id: seriesId,
          chapter_ids: [cbrChapterId],
        });
        return Promise.resolve(response({
          data: {
            asset_id: "123e4567-e89b-42d3-a456-426614174007",
            filename: "Mi_manga - Capitulos 2-2 - 12345678.cbr",
            series_title: "Mi manga",
            chapter_labels: ["Capítulo 2"],
            page_count: 2,
            size_bytes: 128_000,
            sha256: "b".repeat(64),
          },
        }));
      }
      if (path === "/api/v1/usb-exports/targets") {
        return Promise.resolve(response({ data: [] }));
      }
      return Promise.resolve(response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDetail(client);

    fireEvent.click(await screen.findByText("Exportar manga a USB o lector conectado"));
    await screen.findByText("1 capítulo · 2 páginas.");
    fireEvent.click(screen.getByRole("button", { name: "Crear CBR (1 capítulo)" }));
    expect(await screen.findByText(/Mi_manga - Capitulos 2-2/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/cbr-exports", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ series_id: seriesId, chapter_ids: [cbrChapterId] }),
    }));
  });

  it("cleans spacing and publication year while retaining the title and source in chapter search", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const record = seriesRecord(undefined, "yomira.fmd2.webtoons");
    record.display_title = "  Mi título    local (2003)  ";
    record.metadata = record.metadata.map((item) => item.field === "title" ? {
      ...item, value: record.display_title, local_value: record.display_title,
      user_override_value: record.display_title,
    } : item);
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/v1/series/${seriesId}`) {
        return Promise.resolve(response({ data: record, meta: {} }));
      }
      if (path.startsWith(`/api/v1/reader/series/${seriesId}/chapters?`)) {
        return Promise.resolve(response({
          data: [], meta: { limit: 100, offset: 0, total: 0 },
        }));
      }
      return Promise.resolve(response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    }));
    renderDetail(client);

    const searchLink = await screen.findByRole("link", { name: "Buscar capítulos" });
    const onlineLink = screen.getByRole("link", { name: "Elegir fuente para lectura online" });
    const onlineTarget = new URL(onlineLink.getAttribute("href") ?? "", "http://localhost");
    expect(onlineTarget.searchParams.get("q")).toBe("Mi título local");
    expect(onlineTarget.searchParams.has("connector")).toBe(false);
    expect(screen.getByRole("heading", { name: "Leer online" })).toBeInTheDocument();
    const target = new URL(searchLink.getAttribute("href") ?? "", "http://localhost");
    expect(target.pathname).toBe("/search");
    expect(target.searchParams.get("q")).toBe("Mi título local");
    expect(target.searchParams.get("connector")).toBe("yomira.fmd2.webtoons");
  });

  it("searches only on demand and imports an explicitly chosen catalog record", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    client.setQueryData(["library", "all", 0], { fixture: true });
    client.setQueryData(["reader", "progress", "recent", 4], { fixture: true });
    const remoteId = "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a";
    const coloredId = "a787b10a-02d0-46c0-8236-0d01d69ad4a3";
    let record = seriesRecord();
    let finishApply: ((value: Response) => void) | undefined;
    const updated = seriesRecord("2026-10-01T10:01:00Z");
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/metadata/external-candidates")) return Promise.resolve(response({
        data: [
          { remote_id: remoteId, title: "Naruto", year: 1999, author: "Masashi Kishimoto" },
          { remote_id: coloredId, title: "Naruto (Official Colored)", year: 2012, author: null },
        ], meta: { provider: "MangaDex", total: 61, limit: 10 },
      }));
      if (path.endsWith("/metadata/external")) return new Promise<Response>((resolve) => { finishApply = resolve; });
      if (path === `/api/v1/series/${seriesId}`) return Promise.resolve(response({ data: record, meta: {} }));
      return Promise.resolve(response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDetail(client);
    await screen.findByRole("heading", { level: 1, name: "Mi título local" });
    expect(fetchMock.mock.calls.some(([path]) => String(path).includes("metadata/external"))).toBe(false);
    fireEvent.click(metadataButton());
    const candidates = await screen.findByRole("list", { name: "Fichas encontradas en MangaDex" });
    expect(candidates).toHaveTextContent("Naruto · 1999 · Masashi Kishimoto");
    expect(candidates).toHaveTextContent("Naruto (Official Colored) · 2012");
    expect(screen.getByText(/Se muestran hasta 10 de 61 coincidencias/)).toBeInTheDocument();
    expect(screen.getByText(/Solo se completan los campos ausentes/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([path]) => String(path).endsWith("/metadata/external"))).toBe(false);
    const firstCandidate = within(candidates).getAllByRole("listitem")[0];
    if (firstCandidate === undefined) throw new Error("Expected an external metadata candidate");
    fireEvent.click(within(firstCandidate).getByRole("button", { name: "Usar esta ficha" }));
    expect(await screen.findByRole("button", { name: "Actualizando ficha externa…" })).toBeDisabled();
    expect(metadataButton()).toBeDisabled();
    expect(refreshButton()).toBeDisabled();
    for (const button of screen.getAllByRole("button", { name: "Quitar corrección" })) expect(button).toBeDisabled();
    await waitFor(() => expect(finishApply).toBeDefined());
    record = updated;
    finishApply?.(response({ data: updated, meta: {} }));
    expect(await screen.findByText("Ficha externa vinculada; se conservaron tus datos existentes.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("list", { name: "Fichas encontradas en MangaDex" })).not.toBeInTheDocument());
    expect(screen.getByText("Autor elegido por ti")).toBeInTheDocument();
    expect(client.getQueryState(["library", "all", 0])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["reader", "progress", "recent", 4])?.isInvalidated).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/series/${seriesId}/metadata/external`, {
      method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ remote_id: remoteId }),
    });
    expect(Array.from(document.querySelectorAll("img")).every((image) => image.getAttribute("src")?.startsWith("/api/v1/")))
      .toBe(true);
  });

  it("shows a traced catalog error without losing local metadata and permits a new search", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/metadata/external-candidates")) return Promise.resolve(response({ error: {
        code: "SRC_NETWORK_ERROR", message: "El catálogo externo no respondió.", retryable: true,
        trace_id: "trace-metadata-catalog",
      } }, 503));
      return Promise.resolve(path === `/api/v1/series/${seriesId}`
        ? response({ data: seriesRecord(), meta: {} })
        : response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDetail(client);
    await screen.findByRole("heading", { level: 1, name: "Mi título local" });
    fireEvent.click(metadataButton());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("El catálogo externo no respondió.");
    expect(alert).toHaveTextContent("SRC_NETWORK_ERROR");
    expect(alert).toHaveTextContent("trace-metadata-catalog");
    expect(screen.getByText("Autor elegido por ti")).toBeInTheDocument();
    expect(metadataButton()).not.toBeDisabled();
  });

  it("reports an empty catalog result without proposing or applying an arbitrary record", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/metadata/external-candidates")) return Promise.resolve(response({
        data: [], meta: { provider: "MangaDex", total: 0, limit: 10 },
      }));
      return Promise.resolve(path === `/api/v1/series/${seriesId}`
        ? response({ data: seriesRecord(), meta: {} })
        : response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDetail(client);
    await screen.findByRole("heading", { level: 1, name: "Mi título local" });
    fireEvent.click(metadataButton());
    expect(await screen.findByText("No se encontraron fichas en MangaDex para esta serie.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Usar esta ficha" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([path]) => String(path).endsWith("/metadata/external"))).toBe(false);
  });

  it("reloads external metadata saved before a cover failure and keeps the chosen record available for retry", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    client.setQueryData(["library", "recent", 4], { fixture: true });
    const base = seriesRecord();
    let record = {
      ...base, description: null as string | null,
      metadata: base.metadata.map((item) => item.field === "description" ? {
        ...item, value: null, local_value: null, authority: null as string | null,
      } : item),
    };
    const description = "Sinopsis de MangaDex conservada pese al fallo de portada.";
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/metadata/external-candidates")) return Promise.resolve(response({
        data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "Naruto", year: 1999, author: null }],
        meta: { provider: "MangaDex", total: 1, limit: 10 },
      }));
      if (path.endsWith("/metadata/external")) {
        record = {
          ...record, description, updated_at: "2026-10-01T10:01:00Z",
          metadata: record.metadata.map((item) => item.field === "description" ? {
            ...item, value: description, authority: "SOURCE_METADATA", source_values: [{
              series_source_id: secondSeriesId, source_id: coverAssetId,
              source_display_name: "MangaDex", value: description,
            }], effective_source_ids: [coverAssetId],
          } : item),
        };
        return Promise.resolve(response({ error: {
          code: "IMG_DECODE_FAILED", message: "La portada externa no pudo validarse.",
          retryable: false, trace_id: "trace-partial-external-cover",
        } }, 422));
      }
      return Promise.resolve(path === `/api/v1/series/${seriesId}`
        ? response({ data: record, meta: {} })
        : response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDetail(client);
    await screen.findByRole("heading", { level: 1, name: "Mi título local" });
    fireEvent.click(metadataButton());
    fireEvent.click(await screen.findByRole("button", { name: "Usar esta ficha" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("IMG_DECODE_FAILED");
    expect(alert).toHaveTextContent("trace-partial-external-cover");
    await waitFor(() => expect(client.getQueryData(["series", seriesId])).toMatchObject({
      description, coverAssetId,
    }));
    expect(screen.getByText(`MangaDex: ${description}`)).toBeInTheDocument();
    expect(screen.getByText("Autor elegido por ti")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Usar esta ficha" })).not.toBeDisabled();
    expect(client.getQueryState(["library", "recent", 4])?.isInvalidated).toBe(true);
    expect(screen.queryByText("Ficha externa vinculada; se conservaron tus datos existentes.")).not.toBeInTheDocument();
  });

  it("isolates a pending catalog search from the next series", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const base = seriesRecord();
    const secondRecord = {
      ...base, series_id: secondSeriesId, display_title: "Segunda serie",
      metadata: base.metadata.map((item) => item.field === "title" ? {
        ...item, value: "Segunda serie", local_value: "Segunda serie", user_override_value: "Segunda serie",
      } : item),
    };
    let finishSearch: ((value: Response) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/metadata/external-candidates")) return new Promise<Response>((resolve) => { finishSearch = resolve; });
      if (path === `/api/v1/series/${seriesId}`) return Promise.resolve(response({ data: base, meta: {} }));
      if (path === `/api/v1/series/${secondSeriesId}`) return Promise.resolve(response({ data: secondRecord, meta: {} }));
      return Promise.resolve(response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    }));
    renderDetail(client);
    await screen.findByRole("heading", { level: 1, name: "Mi título local" });
    fireEvent.click(metadataButton());
    expect(await screen.findByRole("button", { name: "Buscando datos externos…" })).toBeDisabled();
    expect(refreshButton()).toBeDisabled();
    await waitFor(() => expect(finishSearch).toBeDefined());
    fireEvent.click(screen.getByRole("link", { name: "Abrir segunda serie" }));
    await screen.findByRole("heading", { level: 1, name: "Segunda serie" });
    expect(metadataButton()).not.toBeDisabled();
    finishSearch?.(response({
      data: [{ remote_id: "6b1eb93e-473a-4ab3-9922-1a66d2a29a4a", title: "Naruto", year: 1999, author: null }],
      meta: { provider: "MangaDex", total: 1, limit: 10 },
    }));
    await waitFor(() => expect(screen.getByRole("heading", { level: 1, name: "Segunda serie" })).toBeInTheDocument());
    expect(screen.queryByRole("list", { name: "Fichas encontradas en MangaDex" })).not.toBeInTheDocument();
  });

  it("isolates a new series from a refresh that is still pending for the previous series", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    let firstRecord = seriesRecord();
    const updatedFirstRecord = seriesRecord("2026-09-30T10:01:00Z");
    const base = seriesRecord();
    const secondRecord = {
      ...base, series_id: secondSeriesId, display_title: "Segunda serie",
      metadata: base.metadata.map((item) => item.field === "title" ? {
        ...item, value: "Segunda serie", local_value: "Segunda serie", user_override_value: "Segunda serie",
      } : item),
    };
    let finishRefresh: ((value: Response) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/metadata/refresh")) {
        return new Promise<Response>((resolve) => { finishRefresh = resolve; });
      }
      if (path === `/api/v1/series/${seriesId}`) return Promise.resolve(response({ data: firstRecord, meta: {} }));
      if (path === `/api/v1/series/${secondSeriesId}`) return Promise.resolve(response({ data: secondRecord, meta: {} }));
      return Promise.resolve(response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    }));
    const setQueryData = vi.spyOn(client, "setQueryData");
    renderDetail(client);
    await screen.findByRole("heading", { level: 1, name: "Mi título local" });
    fireEvent.click(refreshButton());
    await screen.findByRole("button", { name: "Actualizando metadatos y portada…" });
    await waitFor(() => expect(finishRefresh).toBeDefined());
    fireEvent.click(screen.getByRole("link", { name: "Abrir segunda serie" }));
    await screen.findByRole("heading", { level: 1, name: "Segunda serie" });
    try {
      expect(refreshButton()).not.toBeDisabled();
      expect(screen.queryByRole("button", { name: "Actualizando metadatos y portada…" }))
        .not.toBeInTheDocument();
    } finally {
      firstRecord = updatedFirstRecord;
      finishRefresh?.(response({ data: updatedFirstRecord, meta: {} }));
    }
    await waitFor(() => expect(setQueryData).toHaveBeenCalledWith(
      ["series", seriesId], expect.objectContaining({ seriesId, updatedAt: updatedFirstRecord.updated_at }),
    ));
    expect(client.getQueryData(["series", secondSeriesId])).toMatchObject({
      seriesId: secondSeriesId, displayTitle: "Segunda serie",
    });
    expect(screen.getByRole("heading", { level: 1, name: "Segunda serie" })).toBeInTheDocument();
    expect(screen.queryByText("Metadatos y portada actualizados.")).not.toBeInTheDocument();
  });

  it("updates the local cover, preserves overrides and invalidates related caches after POST", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    client.setQueryData(["library", "all", 0], { fixture: true });
    client.setQueryData(["library", "recent", 4], { fixture: true });
    client.setQueryData(["reader", "progress", "recent", 4], { fixture: true });
    let record = seriesRecord();
    let finishRefresh: ((value: Response) => void) | undefined;
    const refreshed = seriesRecord("2026-09-30T10:01:00Z");
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/metadata/refresh")) {
        return new Promise<Response>((resolve) => { finishRefresh = resolve; });
      }
      if (path === `/api/v1/series/${seriesId}`) return Promise.resolve(response({ data: record, meta: {} }));
      return Promise.resolve(response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDetail(client);
    await screen.findByRole("heading", { level: 1, name: "Mi título local" });
    expect(screen.getByText("Autor elegido por ti")).toBeInTheDocument();
    expect(screen.getByText("Fantasía, Acción")).toBeInTheDocument();
    expect(screen.getByText("En publicación")).toBeInTheDocument();
    expect(screen.getByText("es")).toBeInTheDocument();
    expect(screen.getAllByText(/Sinopsis guardada/)).toHaveLength(2);
    const image = screen.getByRole("img", { name: "Portada de Mi título local" });
    const initialSource = image.getAttribute("src");
    expect(new URL(initialSource ?? "", "http://127.0.0.1:8000").searchParams.get("variant"))
      .toBe("large");
    fireEvent.error(image);
    fireEvent.click(refreshButton());
    expect(await screen.findByRole("button", { name: "Actualizando metadatos y portada…" }))
      .toBeDisabled();
    for (const button of screen.getAllByRole("button", { name: "Quitar corrección" })) {
      expect(button).toBeDisabled();
    }
    await waitFor(() => expect(finishRefresh).toBeDefined());
    record = refreshed;
    finishRefresh?.(response({ data: refreshed, meta: {} }));
    expect(await screen.findByText("Metadatos y portada actualizados.")).toBeInTheDocument();
    await waitFor(() => expect(refreshButton())
      .not.toBeDisabled());
    expect(screen.getByRole("heading", { level: 1, name: "Mi título local" })).toBeInTheDocument();
    expect(screen.getByText("Autor elegido por ti")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Portada de Mi título local" }).getAttribute("src"))
      .not.toBe(initialSource);
    expect(client.getQueryState(["library", "all", 0])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["library", "recent", 4])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["reader", "progress", "recent", 4])?.isInvalidated).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/series/${seriesId}/metadata/refresh`, {
      headers: { Accept: "application/json" }, method: "POST",
    });
  });

  it("shows the error code and trace while keeping existing metadata and the retry action", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const record = seriesRecord();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/metadata/refresh")) return Promise.resolve(response({ error: {
        code: "SRC_NETWORK_ERROR", message: "La fuente no respondió.", retryable: true,
        trace_id: "trace-metadata-refresh",
      } }, 503));
      if (path === `/api/v1/series/${seriesId}`) return Promise.resolve(response({ data: record, meta: {} }));
      return Promise.resolve(response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    }));
    renderDetail(client);
    await screen.findByRole("heading", { level: 1, name: "Mi título local" });
    fireEvent.click(refreshButton());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("La fuente no respondió.");
    expect(alert).toHaveTextContent("SRC_NETWORK_ERROR");
    expect(alert).toHaveTextContent("trace-metadata-refresh");
    expect(screen.getByRole("heading", { level: 1, name: "Mi título local" })).toBeInTheDocument();
    expect(screen.getByText("Autor elegido por ti")).toBeInTheDocument();
    expect(refreshButton()).not.toBeDisabled();
    expect(screen.queryByText("Metadatos y portada actualizados.")).not.toBeInTheDocument();
  });

  it("reloads metadata saved before a cover failure while preserving the prior cover and error trace", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    client.setQueryData(["library", "all", 0], { fixture: true });
    client.setQueryData(["library", "recent", 4], { fixture: true });
    const base = seriesRecord();
    let record = {
      ...base, description: null as string | null,
      metadata: base.metadata.map((item) => item.field === "description" ? {
        ...item, value: null, local_value: null, authority: null as string | null,
      } : item),
    };
    const description = "Sinopsis nueva guardada antes del fallo de portada.";
    const updatedRecord = {
      ...record, description, updated_at: "2026-09-30T10:01:00Z",
      metadata: record.metadata.map((item) => item.field === "description" ? {
        ...item, value: description, local_value: description, authority: "LOCAL_CANONICAL",
      } : item),
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/metadata/refresh")) {
        record = updatedRecord;
        return Promise.resolve(response({ error: {
          code: "IMG_DECODE_FAILED", message: "La portada recibida no pudo validarse.",
          retryable: false, trace_id: "trace-partial-cover-refresh",
        } }, 422));
      }
      if (path === `/api/v1/series/${seriesId}`) return Promise.resolve(response({ data: record, meta: {} }));
      return Promise.resolve(response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDetail(client);
    await screen.findByRole("heading", { level: 1, name: "Mi título local" });
    fireEvent.click(refreshButton());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("IMG_DECODE_FAILED");
    expect(alert).toHaveTextContent("trace-partial-cover-refresh");
    expect(await screen.findAllByText(description)).toHaveLength(2);
    expect(screen.getByText("Autor elegido por ti")).toBeInTheDocument();
    expect(client.getQueryData(["series", seriesId])).toMatchObject({
      seriesId, description, coverAssetId,
    });
    expect(client.getQueryState(["library", "all", 0])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["library", "recent", 4])?.isInvalidated).toBe(true);
    const cover = screen.getByRole("img", { name: "Portada de Mi título local" });
    const coverUrl = new URL(cover.getAttribute("src") ?? "", "http://127.0.0.1:8000");
    expect(coverUrl.pathname).toBe(`/api/v1/series/${seriesId}/cover`);
    expect(coverUrl.searchParams.get("v")).toBe(`${coverAssetId}:${updatedRecord.updated_at}`);
    expect(screen.queryByText("Metadatos y portada actualizados.")).not.toBeInTheDocument();
    expect(refreshButton()).not.toBeDisabled();
  });

  it("keeps a source-only cover as a placeholder until a local asset exists", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const base = seriesRecord();
    const record = {
      ...base,
      cover_asset_id: null,
      metadata: base.metadata.map((item) => item.field === "cover" ? {
        ...item, value: "https://example.test/remote-cover.webp", local_value: null,
        authority: "SOURCE_METADATA",
      } : item),
    };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(
      String(input) === `/api/v1/series/${seriesId}`
        ? response({ data: record, meta: {} })
        : response({ data: [], meta: { limit: 100, offset: 0, total: 0 } }),
    )));
    renderDetail(client);
    await screen.findByRole("heading", { level: 1, name: "Mi título local" });
    expect(screen.getByRole("img", { name: "Portada disponible en una fuente: Mi título local" }))
      .not.toHaveAttribute("src");
    expect(document.querySelector("img")).toBeNull();
  });
});
