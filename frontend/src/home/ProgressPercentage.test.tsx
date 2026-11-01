import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ProgressPercentage } from "./ProgressPercentage";

afterEach(() => vi.unstubAllGlobals());
it("counts only when visible, reaches the saved value, and shows exact updates when motion is disabled", () => {
  let intersect!: IntersectionObserverCallback; let step!: FrameRequestCallback;
  const disconnect = vi.fn(); const cancel = vi.fn();
  vi.stubGlobal("IntersectionObserver", class { constructor(callback: IntersectionObserverCallback) { intersect = callback; } observe() {} disconnect = disconnect; });
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback) => { step = callback; return 7; }));
  vi.stubGlobal("cancelAnimationFrame", cancel);
  vi.spyOn(performance, "now").mockReturnValue(0);
  const view = render(<ProgressPercentage value={80} animate />);
  expect(screen.getByText("80")).toBeVisible();
  expect(requestAnimationFrame).not.toHaveBeenCalled();
  act(() => intersect([{ isIntersecting: true }] as IntersectionObserverEntry[], {} as IntersectionObserver));
  act(() => step(600));
  expect(screen.getByText("70")).toBeVisible();
  act(() => step(1200));
  expect(screen.getByText("80")).toBeVisible();
  view.rerender(<ProgressPercentage value={25} animate />);
  expect(screen.getByText("25")).toBeVisible();
  act(() => intersect([{ isIntersecting: true }] as IntersectionObserverEntry[], {} as IntersectionObserver));
  act(() => step(300));
  view.rerender(<ProgressPercentage value={25} animate={false} />);
  expect(screen.getByText("25")).toBeVisible();
  expect(cancel).toHaveBeenCalledWith(7);
  view.unmount();
});
it("keeps the exact value when observers are unavailable", () => {
  render(<ProgressPercentage value={37} animate />);
  expect(screen.getByText("37")).toBeVisible();
});
