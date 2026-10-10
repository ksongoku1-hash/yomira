import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CbrExportAction } from "./CbrExportAction";

const seriesId = "123e4567-e89b-42d3-a456-426614174000";
const chapterIds = [
  "123e4567-e89b-42d3-a456-426614174001",
  "123e4567-e89b-42d3-a456-426614174002",
];
const assetId = "123e4567-e89b-42d3-a456-426614174003";

function response(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderAction() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <CbrExportAction seriesId={seriesId} chapterIds={chapterIds} pageCount={4} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("aggregate CBR export", () => {
  it("builds a CBR from selected chapters and sends the verified asset to USB", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/cbr-exports") {
        return Promise.resolve(response({
          data: {
            asset_id: assetId,
            filename: "Mi_manga - Capitulos 2-10 - 12345678.cbr",
            series_title: "Mi manga",
            chapter_labels: ["Capítulo 2", "Capítulo 10"],
            page_count: 4,
            size_bytes: 6_040_488_385,
            sha256: "a".repeat(64),
          },
        }));
      }
      if (path === "/api/v1/usb-exports/targets") {
        return Promise.resolve(response({
          data: [{
            target_id: "BIGME:MTP",
            display_name: "Bigme B6",
            file_system: "MTP",
            total_bytes: 32_000_000_000,
            free_bytes: 20_000_000_000,
          }],
        }));
      }
      if (path === "/api/v1/usb-exports") {
        const body = JSON.parse(String(init?.body)) as { asset_id: string; target_id: string };
        expect(body).toEqual({ asset_id: assetId, target_id: "BIGME:MTP" });
        expect(init?.headers).toMatchObject({ "Idempotency-Key": expect.any(String) });
        return Promise.resolve(response({
          data: {
            export_id: "123e4567-e89b-42d3-a456-426614174004",
            asset_id: assetId,
            target_id: "BIGME:MTP",
            target_label: "Bigme B6",
            file_system: "MTP",
            destination_name: "Mi_manga - Capitulos 2-10 - 12345678.cbr",
            expected_size_bytes: 6_040_488_385,
            expected_sha256: "a".repeat(64),
            status: "EXPORTED",
            created_at: "2026-10-04T15:00:00Z",
            updated_at: "2026-10-04T15:00:01Z",
            started_at: "2026-10-04T15:00:00Z",
            terminal_at: "2026-10-04T15:00:01Z",
            trace_id: "123e4567-e89b-42d3-a456-426614174005",
            error_code: null,
            failure_stage: null,
            retryable: null,
            cause_type: null,
          },
        }));
      }
      return Promise.reject(new Error("Unexpected API request: " + path));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderAction();

    fireEvent.click(screen.getByRole("button", { name: "Crear CBR (2 capítulos)" }));
    expect(await screen.findByText(/CBR creado y verificado/)).toHaveTextContent("4 páginas");
    const target = await screen.findByRole("combobox", { name: "Destino USB o lector" });
    fireEvent.change(target, { target: { value: "BIGME:MTP" } });
    fireEvent.click(screen.getByRole("button", { name: "Exportar CBR a USB" }));

    expect(await screen.findByText("CBR exportado y verificado.")).toBeInTheDocument();
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => String(input) === "/api/v1/usb-exports")).toBe(true);
    });
    const buildCall = fetchMock.mock.calls.find(
      ([input]) => String(input) === "/api/v1/cbr-exports",
    );
    expect(buildCall).toBeDefined();
    expect(JSON.parse(String(buildCall?.[1]?.body))).toEqual({
      series_id: seriesId,
      chapter_ids: chapterIds,
    });
    expect(screen.queryByText(/Users\\\\|Documents\\\\|exports\\\\cbr/i)).not.toBeInTheDocument();
  });
});
