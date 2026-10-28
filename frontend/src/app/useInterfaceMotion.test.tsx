import { act, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { useInterfaceMotion } from "./useInterfaceMotion";

function Page({ enabled = true, extra = false }: { enabled?: boolean; extra?: boolean }) {
  const root = useRef<HTMLElement>(null);
  useInterfaceMotion(root, "/", enabled);
  return <main ref={root}><div data-testid="page"><section data-motion-reveal>
    <ul><li data-motion-card>First</li>{extra ? <li data-motion-card>Second</li> : null}</ul>
  </section></div></main>;
}
afterEach(() => { vi.unstubAllGlobals(); Reflect.deleteProperty(HTMLElement.prototype, "animate"); });

it("reveals visible cards once, observes late data, and cancels animations on reduced motion", async () => {
  let intersect!: IntersectionObserverCallback;
  let change!: () => void;
  const media = { matches: false, addEventListener: vi.fn((_name, listener) => { change = listener; }), removeEventListener: vi.fn() };
  vi.stubGlobal("matchMedia", vi.fn(() => media));
  const observe = vi.fn(); const disconnect = vi.fn(); const unobserve = vi.fn();
  vi.stubGlobal("IntersectionObserver", class { constructor(callback: IntersectionObserverCallback) { intersect = callback; } observe = observe; disconnect = disconnect; unobserve = unobserve; });
  const cancel = vi.fn();
  const animate = vi.fn(() => ({ cancel, finished: new Promise(() => {}) }));
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
  const view = render(<Page />);
  expect(animate).toHaveBeenCalledTimes(1);
  const first = screen.getByText("First");
  act(() => intersect([{ isIntersecting: true, target: first }] as unknown as IntersectionObserverEntry[], {} as IntersectionObserver));
  expect(animate).toHaveBeenLastCalledWith(expect.any(Array), expect.objectContaining({ duration: 650, delay: 0 }));
  act(() => intersect([{ isIntersecting: true, target: first }] as unknown as IntersectionObserverEntry[], {} as IntersectionObserver));
  expect(animate).toHaveBeenCalledTimes(2);
  view.rerender(<Page extra />);
  await act(async () => {});
  const second = screen.getByText("Second");
  expect(observe).toHaveBeenCalledWith(second);
  act(() => intersect([{ isIntersecting: true, target: second }] as unknown as IntersectionObserverEntry[], {} as IntersectionObserver));
  expect(animate).toHaveBeenLastCalledWith(expect.any(Array), expect.objectContaining({ delay: 70 }));
  act(() => { media.matches = true; change(); });
  expect(cancel).toHaveBeenCalledTimes(3);
  act(() => intersect([{ isIntersecting: true, target: second }] as unknown as IntersectionObserverEntry[], {} as IntersectionObserver));
  expect(animate).toHaveBeenCalledTimes(3);
  expect(first).toBeVisible(); expect(second).toBeVisible();
  view.unmount();
  expect(media.removeEventListener).toHaveBeenCalledWith("change", change);
});

it("leaves content visible without motion support or when disabled", () => {
  const view = render(<Page enabled={false} />);
  expect(screen.getByText("First")).toBeVisible();
  view.rerender(<Page />);
  expect(screen.getByText("First")).toBeVisible();
});
