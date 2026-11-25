import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { EpubUsbExport } from "./EpubUsbExport";

const api = vi.hoisted(() => ({ getUsbExportTargets: vi.fn(), prepareSeriesEpubUsbAsset: vi.fn(),
  createUsbAssetExport: vi.fn(), getUsbExportHistoryPage: vi.fn() }));
const connection = vi.hoisted(() => ({ mobile: false }));
vi.mock("../api/client", async original => ({ ...await original<typeof import("../api/client")>(), ...api }));
vi.mock("../settings/MobileAccessPanel", () => ({ get isMobileConnection() { return connection.mobile; } }));
const series = "123e4567-e89b-42d3-a456-426614174000";
const asset = "123e4567-e89b-42d3-a456-426614174001";
const clients: QueryClient[] = [];
function show(disabled = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  const busy = vi.fn();
  render(<QueryClientProvider client={client}><EpubUsbExport seriesId={series} disabled={disabled} onBusyChange={busy} /></QueryClientProvider>);
  return busy;
}
beforeEach(() => {
  vi.resetAllMocks(); connection.mobile = false;
  api.getUsbExportTargets.mockResolvedValue([{ targetId: "reader", displayName: "Bigme", freeBytes: 10 ** 9 }]);
  api.prepareSeriesEpubUsbAsset.mockResolvedValue(asset);
  api.getUsbExportHistoryPage.mockResolvedValue({ data: [] });
  api.createUsbAssetExport.mockResolvedValue({ status: "EXPORTED", assetId: asset });
});
afterEach(() => { clients.forEach(client => client.clear()); clients.length = 0; });

it("detects only on demand, requires a destination, and sends the verified EPUB asset", async () => {
  const busy = show();
  expect(api.getUsbExportTargets).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Enviar al lector"));
  fireEvent.change(await screen.findByRole("combobox", { name: "Lector o unidad USB" }), { target: { value: "reader" } });
  fireEvent.click(screen.getByRole("button", { name: "Enviar EPUB" }));
  expect(await screen.findByText(/EPUB enviado y verificado/)).toBeInTheDocument();
  expect(api.prepareSeriesEpubUsbAsset).toHaveBeenCalledExactlyOnceWith(series);
  expect(api.createUsbAssetExport).toHaveBeenCalledWith(asset, "reader", expect.any(String));
  expect(busy.mock.calls.map(([value]) => value)).toEqual([true, false]);
});

it("keeps failed sends retryable and blocks sends while the book is building", async () => {
  api.prepareSeriesEpubUsbAsset.mockRejectedValueOnce(new Error("disconnected"));
  show(); fireEvent.click(screen.getByText("Enviar al lector"));
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "reader" } });
  fireEvent.click(screen.getByRole("button", { name: "Enviar EPUB" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo enviar el EPUB");
  fireEvent.click(screen.getByRole("button", { name: "Enviar EPUB" }));
  await screen.findByText(/EPUB enviado y verificado/);
  expect(api.prepareSeriesEpubUsbAsset).toHaveBeenCalledTimes(2);
});

it("does not query USB devices from a mobile connection", async () => {
  connection.mobile = true; show();
  await waitFor(() => expect(screen.queryByText("Enviar al lector")).not.toBeInTheDocument());
  expect(api.getUsbExportTargets).not.toHaveBeenCalled();
});

it("prevents an export while the EPUB is being rebuilt", async () => {
  show(true); fireEvent.click(screen.getByText("Enviar al lector"));
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "reader" } });
  expect(screen.getByRole("button", { name: "Enviar EPUB" })).toBeDisabled();
});
