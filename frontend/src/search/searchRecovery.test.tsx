import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { SearchPage, SearchResultCard } from "./SearchPage";
import type { ConnectorDescriptor } from "../api/client";

const connector = { connectorId: "yomira.fmd2.webtoons", connectorVersion: "1.0.0",
  capabilities: ["SEARCH", "METADATA"] as ("SEARCH" | "METADATA")[] };
function response(payload: unknown): Response {
  return { ok: true, status: 200, headers: { get: () => null }, json: async () => payload } as unknown as Response;
}
function setup(gcTime = 0) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime, staleTime: Infinity } } });
  client.setQueryData(["connectors"], { data: [connector] });
  return client;
}
afterEach(() => vi.unstubAllGlobals());

it("keeps the chosen screen if an online registration finishes after leaving search", async () => {
  let finishReading!: (value: Response) => void;
  const readingConnector: ConnectorDescriptor = { ...connector,
    capabilities: ["SEARCH", "METADATA", "CHAPTER_LIST", "PAGE_LIST"],
  };
  const result = { remoteId: "fixture", remoteUrl: null, remoteTitle: "Fixture",
    coverUrl: "https://example.test/cover.jpg", sourceLabel: "Source" };
  const group = { key: "title:fixture", title: "Fixture", coverUrl: result.coverUrl,
    coverLookup: null, sources: [{ connector: readingConnector, result, key: "source" }] };
  const fetch = vi.fn((input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/reader-chapter")) return new Promise<Response>((resolve) => { finishReading = resolve; });
    if (path.endsWith("/availability")) return Promise.resolve(response({ data: {
      linked: false, ready_remote_ids: [], ready_labels: [], busy_remote_ids: [],
    } }));
    return Promise.resolve(response({ data: [{ remote_id: "chapter-1", display_label: "Capítulo 1",
      remote_label: "1", chapter_raw_label: "1", chapter_sort_key: [1], language: "es",
    }], meta: { connector_id: readingConnector.connectorId, remote_series_id: "fixture" } }));
  });
  vi.stubGlobal("fetch", fetch);
  const client = setup();
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/search"]}>
    <Link to="/library">Ir a Biblioteca</Link><Routes>
      <Route path="/search" element={<ul><SearchResultCard group={group} expandedKey="title:fixture|source" onToggleSource={() => {}} /></ul>} />
      <Route path="/library" element={<h1>Biblioteca</h1>} />
      <Route path="/reader/:chapterId" element={<h1>Lector</h1>} />
    </Routes>
  </MemoryRouter></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Leer online Capítulo 1" }));
  await waitFor(() => expect(finishReading).toBeDefined());
  fireEvent.click(screen.getByRole("link", { name: "Ir a Biblioteca" }));
  await screen.findByRole("heading", { name: "Biblioteca" });
  await act(async () => finishReading(response({ data: {
    chapter_id: "123e4567-e89b-42d3-a456-426614174001", series_id: "123e4567-e89b-42d3-a456-426614174000",
    display_label: "Capítulo 1",
  }, meta: { connector_id: readingConnector.connectorId, remote_series_id: "fixture" } })));
  expect(screen.getByRole("heading", { name: "Biblioteca" })).toBeInTheDocument();
});

it("permits replacing a search while one provider has not answered", async () => {
  const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("Cancelled", "AbortError")), { once: true });
  }));
  vi.stubGlobal("fetch", fetch);
  const client = setup();
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/search?q=Naruto"]}><SearchPage /></MemoryRouter></QueryClientProvider>);
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "One Piece" } });
  const button = screen.getByRole("button", { name: "Buscar" });
  expect(button).toBeEnabled();
  const previousSignal = fetch.mock.calls[0]?.[1]?.signal;
  fireEvent.click(button);
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  expect(previousSignal?.aborted).toBe(true);
  expect(String(fetch.mock.calls[1]?.[0])).toContain("query=One+Piece");
});

it("displays a replacement cover after an earlier provider image failed", () => {
  const client = setup();
  const source = { connector, result: { remoteId: "series", remoteUrl: null, remoteTitle: "Naruto",
    coverUrl: "https://source.example/broken.jpg", sourceLabel: "Source" }, key: "source" };
  const group = { key: "title:naruto", title: "Naruto", coverUrl: "https://source.example/broken.jpg", coverLookup: null, sources: [source] };
  const view = (coverUrl: string) => <QueryClientProvider client={client}><MemoryRouter><ul>
    <SearchResultCard group={{ ...group, coverUrl }} expandedKey={null} onToggleSource={() => {}} />
  </ul></MemoryRouter></QueryClientProvider>;
  const rendered = render(view(group.coverUrl));
  fireEvent.error(screen.getByRole("img", { name: "Portada de Naruto" }));
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  rendered.rerender(view("https://mangadex.example/valid.jpg"));
  expect(screen.getByRole("img", { name: "Portada de Naruto" })).toHaveAttribute("src", "https://mangadex.example/valid.jpg");
});

it("aborts an unused cover lookup when leaving search results", async () => {
  let coverSignal: AbortSignal | null | undefined;
  vi.stubGlobal("IntersectionObserver", undefined);
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes("/search?")) return Promise.resolve(response({ data: [{
      remote_id: "fixture", remote_url: null, remote_title: "Naruto", cover_url: null,
    }], meta: { connector_id: connector.connectorId, query: "Naruto" } }));
    coverSignal = init?.signal;
    return new Promise<Response>(() => {});
  });
  vi.stubGlobal("fetch", fetch);
  const client = setup();
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/search?q=Naruto"]}><SearchPage /></MemoryRouter></QueryClientProvider>);
  await waitFor(() => expect(fetch.mock.calls.some((call) => String(call[0]).includes("/cover?"))).toBe(true));
  view.unmount();
  expect(coverSignal?.aborted).toBe(true);
});

it("retries a cached missing cover when returning to its search", async () => {
  vi.stubGlobal("IntersectionObserver", undefined);
  let coverCalls = 0;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname.endsWith("/search")) {
      const query = url.searchParams.get("query");
      return Promise.resolve(response({ data: query === "Naruto" ? [{
        remote_id: "naruto", remote_url: null, remote_title: "Naruto", cover_url: null,
      }] : [], meta: { connector_id: connector.connectorId, query } }));
    }
    coverCalls++;
    return Promise.resolve(response({
      data: { cover_url: coverCalls === 1 ? null : "https://uploads.mangadex.org/recovered.jpg" },
      meta: { connector_id: connector.connectorId, remote_series_id: "naruto" },
    }));
  }));
  const client = setup(60_000);
  const view = render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={["/search?q=Naruto"]}><SearchPage /></MemoryRouter>
  </QueryClientProvider>);
  try {
    await waitFor(() => expect(coverCalls).toBe(1));
    await screen.findByText("Sin portada");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "One Piece" } });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Naruto" }))
      .not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Buscar" })).toBeEnabled());
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "Naruto" } });
    fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
    expect(await screen.findByRole("img", { name: "Portada de Naruto" }))
      .toHaveAttribute("src", "https://uploads.mangadex.org/recovered.jpg");
    expect(coverCalls).toBe(2);
  } finally {
    view.unmount();
    client.clear();
  }
});

it("refetches an unchanged search without reloading a successful cover", async () => {
  vi.stubGlobal("IntersectionObserver", undefined);
  let searches = 0;
  let covers = 0;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    if (String(input).includes("/search?")) {
      searches++;
      return Promise.resolve(response({ data: [{
        remote_id: "naruto", remote_url: null, remote_title: "Naruto", cover_url: null,
      }], meta: { connector_id: connector.connectorId, query: "Naruto" } }));
    }
    covers++;
    return Promise.resolve(response({
      data: { cover_url: "https://uploads.mangadex.org/successful.jpg" },
      meta: { connector_id: connector.connectorId, remote_series_id: "naruto" },
    }));
  }));
  const client = setup();
  render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={["/search?q=Naruto"]}><SearchPage /></MemoryRouter>
  </QueryClientProvider>);
  await screen.findByRole("img", { name: "Portada de Naruto" });
  expect(searches).toBe(1);
  expect(covers).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Buscar" }));
  await waitFor(() => expect(searches).toBe(2));
  await waitFor(() => expect(screen.getByRole("button", { name: "Buscar" })).toBeEnabled());
  expect(screen.getByRole("img", { name: "Portada de Naruto" }))
    .toHaveAttribute("src", "https://uploads.mangadex.org/successful.jpg");
  expect(covers).toBe(1);
});

it("does not fetch a whole catalog for a search title longer than the cover lookup limit", async () => {
  let showCover: (() => void) | undefined;
  vi.stubGlobal("IntersectionObserver", class {
    constructor(callback: (entries: { isIntersecting: boolean }[]) => void) {
      showCover = () => callback([{ isIntersecting: true }]);
    }
    observe() {}
    disconnect() {}
  });
  const title = "Naruto " + "a".repeat(194);
  const fetch = vi.fn((input: RequestInfo | URL) => Promise.resolve(response(
    String(input).includes("/search?") ? { data: [{
      remote_id: "long-title", remote_url: null, remote_title: title, cover_url: null,
    }], meta: { connector_id: connector.connectorId, query: "Naruto" } } : {
      data: { cover_url: null },
      meta: { connector_id: connector.connectorId, remote_series_id: "long-title" },
    },
  )));
  vi.stubGlobal("fetch", fetch);
  const client = setup();
  render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={["/search?q=Naruto"]}><SearchPage /></MemoryRouter>
  </QueryClientProvider>);
  await screen.findByRole("heading", { name: title });
  await waitFor(() => expect(showCover).toBeDefined());
  await act(async () => { showCover?.(); });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Sin portada")).toBeInTheDocument();
});
