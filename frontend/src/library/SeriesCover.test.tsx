import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LibrarySeriesSummary } from "../api/client";
import { HomePage } from "../home/HomePage";
import { LibraryPage } from "./LibraryPage";
import { SeriesCover } from "./SeriesCover";

const series: LibrarySeriesSummary = {
  seriesId: "123e4567-e89b-42d3-a456-426614174000",
  canonicalTitle: "berserk",
  displayTitle: "Berserk",
  author: "Kentaro Miura",
  genres: ["Fantasía", "Acción"],
  description: "Una historia guardada en tu biblioteca.",
  status: "ongoing",
  language: "ja",
  coverAssetId: "123e4567-e89b-42d3-a456-426614174001",
  createdAt: "2026-09-30T10:00:00Z",
  updatedAt: "2026-09-30T10:00:00Z",
};

const wireSeries = {
  series_id: series.seriesId,
  canonical_title: series.canonicalTitle,
  display_title: series.displayTitle,
  author: series.author,
  genres: series.genres,
  description: series.description,
  status: series.status,
  language: series.language,
  cover_asset_id: series.coverAssetId,
  created_at: series.createdAt,
  updated_at: series.updatedAt,
};

function response(payload: unknown): Response {
  return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
}

function renderPage(page: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{page}</MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("local series covers", () => {
  it("uses a lazy local image with an accessible title and a bounded variant", () => {
    render(<SeriesCover series={series} />);
    const image = screen.getByRole("img", { name: "Portada de Berserk" });
    const url = new URL(image.getAttribute("src") ?? "", "http://127.0.0.1:8000");
    expect(url.pathname).toBe(`/api/v1/series/${series.seriesId}/cover`);
    expect(url.searchParams.get("variant")).toBe("medium");
    expect(url.searchParams.get("v")).toBe(`${series.coverAssetId}:${series.updatedAt}`);
    expect(image).toHaveAttribute("loading", "lazy");
    expect(image).toHaveAttribute("decoding", "async");
  });

  it("falls back after an image failure and retries when its local version changes", () => {
    const view = render(<SeriesCover series={series} />);
    const image = screen.getByRole("img", { name: "Portada de Berserk" });
    const originalSource = image.getAttribute("src");
    fireEvent.error(image);
    expect(screen.getByRole("img", { name: "Vista previa no disponible: Berserk" }))
      .not.toHaveAttribute("src");
    view.rerender(<SeriesCover series={{ ...series, updatedAt: "2026-09-30T10:01:00Z" }} />);
    expect(screen.getByRole("img", { name: "Portada de Berserk" }).getAttribute("src"))
      .not.toBe(originalSource);
  });

  it("keeps an accessible placeholder when no validated local asset is assigned", () => {
    render(<SeriesCover series={{ ...series, coverAssetId: null }} />);
    expect(screen.getByRole("img", { name: "Sin portada asignada: Berserk" }))
      .not.toHaveAttribute("src");
    expect(document.querySelector("img")).toBeNull();
  });

  it("renders library covers beside the effective author, genres and synopsis", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      data: [wireSeries], meta: { limit: 100, offset: 0, total: 1 },
    })));
    renderPage(<LibraryPage />);
    expect(await screen.findByRole("heading", { name: "Berserk" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Portada de Berserk" })).toHaveAttribute("loading", "lazy");
    expect(screen.getByText("Autor: Kentaro Miura")).toBeInTheDocument();
    expect(screen.getByText("Géneros: Fantasía, Acción")).toBeInTheDocument();
    expect(screen.getByText("Publicación: En publicación")).toBeInTheDocument();
    expect(screen.getByText("Idioma: ja")).toBeInTheDocument();
    expect(screen.getByText(series.description ?? "")).toBeInTheDocument();
  });

  it("uses medium covers in recent home cards while keeping their navigation label", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/system/readiness") return Promise.resolve(response({ status: "ready" }));
      if (path.startsWith("/api/v1/library/recent")) {
        return Promise.resolve(response({ data: [wireSeries], meta: { limit: 4 } }));
      }
      return Promise.resolve(response({ data: [], meta: { limit: 4 } }));
    }));
    renderPage(<HomePage />);
    const link = await screen.findByRole("link", { name: "Berserk" });
    expect(link).toHaveAttribute("href", `/series/${series.seriesId}`);
    const image = screen.getByRole("img", { name: "Portada de Berserk" });
    expect(new URL(image.getAttribute("src") ?? "", "http://127.0.0.1:8000").searchParams.get("variant"))
      .toBe("medium");
    expect(getComputedStyle(image).width).toBe("100%");
    fireEvent.error(image);
    const fallback = screen.getByRole("img", { name: "Vista previa no disponible: Berserk" });
    expect(getComputedStyle(fallback).width).toBe("100%");
    expect(screen.getByText("Autor: Kentaro Miura")).toBeInTheDocument();
  });
});
