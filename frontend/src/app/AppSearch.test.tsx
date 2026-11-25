import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it } from "vitest";
import { AppSearch } from "./AppSearch";
import { useAppearanceStore } from "./appearanceStore";

afterEach(() => { useAppearanceStore.getState().update({ theme: "tinta" }); localStorage.clear(); });

it("shares the focusable header theme switch with saved appearance settings", () => {
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><AppSearch ready={false} /></MemoryRouter></QueryClientProvider>);
  const toggle = screen.getByRole("button", { name: "Tema claro" });
  expect(toggle).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-pressed", "true");
  expect(JSON.parse(localStorage.getItem("yomira.appearance.v1")!).theme).toBe("papel");
  act(() => useAppearanceStore.getState().update({ theme: "tinta" }));
  expect(toggle).toHaveAttribute("aria-pressed", "false");
  toggle.focus(); expect(toggle).toHaveFocus();
  fireEvent.click(toggle); expect(toggle).toHaveAttribute("aria-pressed", "true");
});
