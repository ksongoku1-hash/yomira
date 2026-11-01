import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReaderCachePanel } from "./ReaderCachePanel";

function response(limit = 1024) {
  return new Response(JSON.stringify({ data: { limit_mb: limit, used_bytes: 2 * 1024**2, entries: 4, file_bytes: 3 * 1024**2 }, meta: {} }), { headers: { "content-type": "application/json" } });
}
function open() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}><ReaderCachePanel /></QueryClientProvider>);
}
afterEach(() => vi.unstubAllGlobals());
describe("disk image cache settings", () => {
  it("shows usage, rejects invalid limits and persists a valid change", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => response(init?.method === "PUT" ? 2048 : 1024));
    vi.stubGlobal("fetch", fetchMock);
    open();
    const input = await screen.findByRole("spinbutton", { name: "Límite en disco (MB)" });
    expect(screen.getByText("Imágenes almacenadas: 2 MB · 4 páginas")).toBeInTheDocument();
    expect(screen.getByText(/el espacio del archivo se reutiliza/)).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "8193" } });
    expect(screen.getByRole("button", { name: "Guardar límite de caché" })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.change(input, { target: { value: "2048" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar límite de caché" }));
    await screen.findByText("Límite de caché guardado.");
    expect(fetchMock).toHaveBeenLastCalledWith("/api/v1/settings/reader-cache", expect.objectContaining({ method: "PUT", body: '{"limit_mb":2048}' }));
    expect(input).toHaveValue(2048);
  });
  it("keeps the entered limit when saving fails and allows retry", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response()).mockRejectedValueOnce(new Error("Sin conexión")).mockResolvedValueOnce(response(2048));
    vi.stubGlobal("fetch", fetchMock);
    open();
    const input = await screen.findByRole("spinbutton", { name: "Límite en disco (MB)" });
    fireEvent.change(input, { target: { value: "2048" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar límite de caché" }));
    await screen.findByRole("alert");
    expect(input).toHaveValue(2048);
    fireEvent.click(screen.getByRole("button", { name: "Guardar límite de caché" }));
    await screen.findByText("Límite de caché guardado.");
  });
  it("rejects malformed cache responses and offers a reload", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response('{"data":{"limit_mb":true}}')).mockResolvedValueOnce(response()));
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Reintentar caché" }));
    await waitFor(() => expect(screen.getByRole("spinbutton")).toHaveValue(1024));
  });
});
