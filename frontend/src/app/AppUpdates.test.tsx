import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { appUpdates } from "../api/client";
import type { AppUpdateStatus } from "../api/client";
import { AppUpdates } from "./AppUpdates";

vi.mock("../api/client", () => ({ appUpdates: vi.fn(), getSystemVersion: vi.fn() }));
const base: AppUpdateStatus = { state: "idle", current_version: "0.1.14", latest_version: null,
  downloaded_bytes: 0, total_bytes: 0, message: "", releases_url: "https://github.com/ksongoku1-hash/yomira/releases" };

function show(status: Partial<AppUpdateStatus> = {}, props = {}) {
  vi.mocked(appUpdates).mockResolvedValue({ ...base, ...status });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><AppUpdates {...props} /></QueryClientProvider>);
}

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

describe("AppUpdates", () => {
  it("checks only on explicit click and shows automatic schedule", async () => {
    show();
    expect(await screen.findByText(/Versión instalada: 0.1.14/)).toBeVisible();
    expect(screen.getByText(/cada seis horas/)).toBeVisible();
    expect(appUpdates).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Buscar actualizaciones" }));
    await waitFor(() => expect(appUpdates).toHaveBeenCalledWith("check", expect.anything()));
  });
  it("shows progress and prevents install until verified", async () => {
    show({ state: "downloading", downloaded_bytes: 40, total_bytes: 100 });
    expect(await screen.findByRole("progressbar")).toHaveAttribute("value", "40");
    expect(screen.queryByRole("button", { name: "Reiniciar y actualizar" })).toBeNull();
    expect(screen.getByRole("button")).toBeDisabled();
  });
  it("installs only on click and switches to restart feedback", async () => {
    show({ state: "ready", latest_version: "0.1.15" });
    const button = await screen.findByRole("button", { name: "Reiniciar y actualizar" });
    expect(appUpdates).toHaveBeenCalledTimes(1);
    vi.mocked(appUpdates).mockResolvedValue({ ...base, state: "installing", latest_version: "0.1.15" });
    fireEvent.click(button);
    expect(await screen.findByText(/volverá a abrirse automáticamente/)).toBeVisible();
    expect(appUpdates).toHaveBeenCalledWith("install", expect.anything());
  });
  it("explains manual updates for source and portable copies", async () => {
    show({ state: "unsupported", message: "Requiere la versión instalada de Windows." });
    expect(await screen.findByText(/Requiere la versión instalada/)).toBeVisible();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByRole("link", { name: "Ver versiones en GitHub" })).toHaveAttribute("rel", "noreferrer");
  });
  it("hides the notice while reading", async () => {
    const { container } = show({ state: "ready" }, { compact: true, hidden: true });
    await waitFor(() => expect(appUpdates).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
  it("handles failed restart request without losing the ready notice", async () => {
    show({ state: "ready" });
    const button = await screen.findByRole("button", { name: "Reiniciar y actualizar" });
    vi.mocked(appUpdates).mockRejectedValue(new Error("offline"));
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Vuelve a intentarlo/);
    expect(button).toBeEnabled();
  });
});
