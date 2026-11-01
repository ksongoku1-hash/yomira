import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { MobileAccessPanel } from "./MobileAccessPanel";

afterEach(() => vi.unstubAllGlobals());

function show() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: {
    queries: { retry: false }, mutations: { retry: false },
  } })}><MobileAccessPanel /></QueryClientProvider>);
}

function reply(value: unknown, status = 200) {
  return { ok: status === 200, status, json: async () => value } as Response;
}

it("activates access, keeps PIN hidden and closes sessions when disabled", async () => {
  const off = { enabled: false, local: true, addresses: ["192.168.1.29"] };
  const on = { ...off, enabled: true, url: "http://192.168.1.29:5183", pin: "234567" };
  const fetch = vi.fn().mockResolvedValueOnce(reply(off)).mockResolvedValueOnce(reply(on))
    .mockResolvedValueOnce(reply(off));
  vi.stubGlobal("fetch", fetch);
  show();
  fireEvent.click(await screen.findByRole("button", { name: "Activar acceso desde móvil" }));
  expect(await screen.findByDisplayValue(on.url)).toHaveAttribute("readonly");
  expect(screen.queryByText(on.pin)).not.toBeInTheDocument();
  expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/system/mobile/enable", expect.objectContaining({
    method: "POST", body: JSON.stringify({ address: "192.168.1.29" }),
    headers: { "Content-Type": "application/json", "X-Yomira-Mobile": "1" },
  }));
  fireEvent.click(screen.getByRole("button", { name: "Mostrar PIN" }));
  expect(screen.getByText(on.pin)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Desactivar y cerrar sesiones" }));
  await screen.findByRole("button", { name: "Activar acceso desde móvil" });
  expect(screen.queryByText(on.pin)).not.toBeInTheDocument();
});

it("offers logout without PC controls on the mobile session", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply({ enabled: true, local: false })));
  show();
  const logout = await screen.findByRole("button", { name: "Cerrar sesión del móvil" });
  expect(logout.closest("form")).toHaveAttribute("action", "/mobile/logout");
  expect(screen.queryByRole("button", { name: "Mostrar PIN" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Permitir conexión en Windows" })).not.toBeInTheDocument();
  expect(screen.queryByRole("checkbox", { name: "Activar el acceso móvil al iniciar Yomira" })).not.toBeInTheDocument();
});

it("saves automatic startup and restores the saved choice when the panel opens again", async () => {
  const initial = { enabled: true, local: true, addresses: ["192.168.1.29"],
    auto_start: false, url: "http://192.168.1.29:5183", pin: "234567" };
  const saved = { ...initial, auto_start: true };
  const fetch = vi.fn().mockResolvedValueOnce(reply(initial)).mockResolvedValueOnce(reply(saved))
    .mockResolvedValue(reply(saved));
  vi.stubGlobal("fetch", fetch);
  const view = show();
  const toggle = await screen.findByRole("checkbox", { name: "Activar el acceso móvil al iniciar Yomira" });
  expect(toggle).not.toBeChecked();
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle).toBeChecked());
  expect(fetch).toHaveBeenNthCalledWith(2, "/api/v1/system/mobile/startup", expect.objectContaining({
    method: "POST", body: JSON.stringify({ address: "192.168.1.29", enabled: true }),
  }));
  view.unmount();
  show();
  expect(await screen.findByRole("checkbox", { name: "Activar el acceso móvil al iniciar Yomira" })).toBeChecked();
  expect(screen.queryByText(initial.pin)).not.toBeInTheDocument();
});

it("keeps the previous startup choice visible if saving fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(reply({ enabled: true, local: true,
    auto_start: true, addresses: ["192.168.1.29"] })).mockResolvedValueOnce(reply({}, 503)));
  show();
  const toggle = await screen.findByRole("checkbox", { name: "Activar el acceso móvil al iniciar Yomira" });
  fireEvent.click(toggle);
  await screen.findByRole("alert");
  expect(toggle).toBeChecked();
});

it("shows connection failures without claiming access is active", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(reply({ enabled: false, local: true,
    addresses: ["192.168.1.29"] })).mockResolvedValueOnce(reply({}, 409)));
  show();
  fireEvent.click(await screen.findByRole("button", { name: "Activar acceso desde móvil" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cambiar la conexión");
  expect(screen.queryByRole("button", { name: "Mostrar PIN" })).not.toBeInTheDocument();
});

it("only requests the scoped Windows permission after clicking its button", async () => {
  const fetch = vi.fn().mockResolvedValueOnce(reply({ enabled: true, local: true,
    addresses: ["192.168.1.29"], url: "http://192.168.1.29:5183", pin: "234567" }))
    .mockResolvedValueOnce(reply({ message: "OK" }));
  vi.stubGlobal("fetch", fetch);
  show();
  const button = await screen.findByRole("button", { name: "Permitir conexión en Windows" });
  expect(fetch).toHaveBeenCalledTimes(1);
  fireEvent.click(button);
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Acepta el aviso de Windows"));
  expect(fetch).toHaveBeenLastCalledWith("/api/v1/system/mobile/firewall", {
    method: "POST", headers: { "X-Yomira-Mobile": "1" },
  });
});

it("does not offer the Windows firewall action on Linux", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply({ enabled: true, local: true,
    addresses: ["192.168.1.29"], url: "http://192.168.1.29:5183", pin: "234567",
    windows_firewall_supported: false })));
  show();
  await screen.findByRole("button", { name: "Mostrar PIN" });
  expect(screen.queryByRole("button", { name: "Permitir conexión en Windows" })).not.toBeInTheDocument();
  expect(screen.getByText(/cortafuegos de tu distribución de Linux/)).toBeInTheDocument();
});
