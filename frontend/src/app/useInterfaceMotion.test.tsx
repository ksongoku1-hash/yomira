import { act, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { useInterfaceMotion } from "./useInterfaceMotion";

function Page({ enabled = true, extra = false }: { enabled?: boolean; extra?: boolean }) {
  const root = useRef<HTMLElement>(null);
  useInterfaceMotion(root, "/", enabled);
  return <main ref={root}><div data-testid="page"><section data-motion-reveal>
    <ul><li data-motion-card data-motion-surface="cover"><span>First</span></li>{extra ? <li data-motion-card>Second</li> : null}</ul>
    <button type="button"><span>Action</span></button>
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
  const first = screen.getByText("First").closest<HTMLElement>("[data-motion-card]")!;
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

function move(element: HTMLElement, pointerType = "mouse", clientX = 80, clientY = 20) {
  const event = new Event("pointermove", { bubbles: true });
  Object.assign(event, { pointerType, clientX, clientY });
  fireEvent(element, event);
}

it("batches pointer light and tilt, leaves touch alone, and clears styles on exit and disable", () => {
  const reduced = { matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() };
  const fine = { ...reduced, matches: true };
  vi.stubGlobal("matchMedia", vi.fn((query: string) => query.includes("reduced-motion") ? reduced : fine));
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: () => ({ cancel: vi.fn(), finished: new Promise(() => {}) }) });
  let flush!: FrameRequestCallback;
  const request = vi.fn((callback: FrameRequestCallback) => { flush = callback; return 1; });
  const cancel = vi.fn();
  vi.stubGlobal("requestAnimationFrame", request); vi.stubGlobal("cancelAnimationFrame", cancel);
  const view = render(<Page />);
  const child = screen.getByText("First");
  const card = child.closest<HTMLElement>("[data-motion-surface]")!;
  vi.spyOn(card, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, toJSON: () => ({}) });
  move(child, "touch"); expect(request).not.toHaveBeenCalled();
  move(child); move(child, "mouse", 200, -100);
  expect(request).toHaveBeenCalledTimes(1);
  act(() => flush(0));
  expect(card).toHaveAttribute("data-motion-active", "true");
  expect(card.style.getPropertyValue("--surface-x")).toBe("100%");
  expect(card.style.getPropertyValue("--surface-y")).toBe("0%");
  expect(card.style.getPropertyValue("--surface-rx")).toBe("5deg");
  const out = new Event("pointerout", { bubbles: true }); Object.assign(out, { relatedTarget: document.body });
  fireEvent(card, out);
  expect(card).not.toHaveAttribute("data-motion-active"); expect(card.style.length).toBe(0);
  move(child); view.rerender(<Page enabled={false} />);
  act(() => flush(0));
  expect(card).not.toHaveAttribute("data-motion-active"); expect(cancel).toHaveBeenCalledWith(1);
  view.unmount();
});

it.each(["reduced motion", "coarse pointer"])("does not schedule cursor effects with %s", (preference) => {
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: query.includes("reduced-motion") ? preference === "reduced motion" : false,
    addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: () => ({ cancel: vi.fn(), finished: new Promise(() => {}) }) });
  const request = vi.fn(); vi.stubGlobal("requestAnimationFrame", request);
  const view = render(<Page />); move(screen.getByText("First"));
  expect(request).not.toHaveBeenCalled(); view.unmount();
});

it("clears the active surface immediately when reduced motion changes", () => {
  let change!: () => void;
  const reduced = { matches: false, addEventListener: vi.fn((_name, listener) => { change = listener; }), removeEventListener: vi.fn() };
  vi.stubGlobal("matchMedia", vi.fn((query: string) => query.includes("reduced-motion") ? reduced : { ...reduced, matches: true, addEventListener: vi.fn() }));
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: () => ({ cancel: vi.fn(), finished: new Promise(() => {}) }) });
  let flush!: FrameRequestCallback;
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => { flush = callback; return 1; }));
  const view = render(<Page />); const button = screen.getByRole("button");
  vi.spyOn(button, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 100, height: 100 } as DOMRect);
  move(button); act(() => flush(0)); expect(button).toHaveAttribute("data-motion-active");
  act(() => { reduced.matches = true; change(); });
  expect(button).not.toHaveAttribute("data-motion-active"); expect(button.style.length).toBe(0);
  view.unmount();
});

function pressAnimations() {
  const records: { cancel: ReturnType<typeof vi.fn>; finish: () => void }[] = [];
  const animate = vi.fn(() => {
    let finish!: () => void;
    const finished = new Promise<void>((resolve) => { finish = resolve; });
    const cancel = vi.fn(); records.push({ cancel, finish });
    return { cancel, finished };
  });
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
  return { animate, records };
}

function MotionDialog({ transform }: { transform: string }) {
  const root = useRef<HTMLElement>(null);
  useInterfaceMotion(root, "/", true);
  return <main ref={root}><dialog open style={{ transform }}><button type="button">Dialog action</button></dialog></main>;
}

it("skips the wave while a dialog is transformed and mounts it in the dialog after its entrance", () => {
  pressAnimations();
  const view = render(<MotionDialog transform="translateY(10px)" />);
  const button = screen.getByRole("button", { name: "Dialog action" });
  vi.spyOn(button, "getBoundingClientRect").mockReturnValue({ left: 20, top: 40, width: 100, height: 40 } as DOMRect);
  fireEvent.click(button);
  expect(document.querySelector("[data-motion-press]")).toBeNull();
  view.rerender(<MotionDialog transform="none" />);
  fireEvent.click(button);
  expect(screen.getByRole("dialog")).toContainElement(document.querySelector("[data-motion-press]"));
  view.unmount();
  expect(document.querySelector("[data-motion-press]")).toBeNull();
});

it("shows one bounded press wave for keyboard and touch activation and removes it when finished", async () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  const { animate, records } = pressAnimations();
  const view = render(<Page />); const button = screen.getByRole("button");
  vi.spyOn(button, "getBoundingClientRect").mockReturnValue({ left: 10, top: 20, width: 100, height: 40 } as DOMRect);
  fireEvent.click(screen.getByText("Action"), { detail: 0 });
  const first = document.querySelector<HTMLElement>("[data-motion-press]")!;
  expect(first).toHaveAttribute("aria-hidden", "true");
  expect(first.style.left).toBe("10px"); expect(first.style.top).toBe("20px");
  expect((first.firstElementChild as HTMLElement).style.left).toBe("50px");
  expect((first.firstElementChild as HTMLElement).style.top).toBe("20px");
  expect(animate).toHaveBeenLastCalledWith(expect.any(Array), { duration: 420, easing: "cubic-bezier(.2,.7,.2,1)" });
  fireEvent.click(button, { detail: 1, clientX: 500, clientY: -20 });
  expect(records[1]!.cancel).toHaveBeenCalledOnce();
  expect(document.querySelectorAll("[data-motion-press]")).toHaveLength(1);
  const next = document.querySelector<HTMLElement>("[data-motion-press]")!;
  expect(next).not.toBe(first);
  expect((next.firstElementChild as HTMLElement).style.left).toBe("100px");
  expect((next.firstElementChild as HTMLElement).style.top).toBe("0px");
  await act(async () => records[1]!.finish());
  expect(document.querySelector("[data-motion-press]")).toBe(next);
  await act(async () => records[2]!.finish());
  expect(document.querySelector("[data-motion-press]")).toBeNull();
  view.unmount();
});

it("cancels press feedback on panel scroll, disable, hiding or reduced motion, and ignores disabled controls", () => {
  let change!: () => void;
  const reduced = { matches: false, addEventListener: vi.fn((_name, listener) => { change = listener; }), removeEventListener: vi.fn() };
  vi.stubGlobal("matchMedia", vi.fn((query: string) => query.includes("reduced-motion") ? reduced : { ...reduced, matches: false, addEventListener: vi.fn() }));
  const { records } = pressAnimations();
  const view = render(<Page />); const button = screen.getByRole("button");
  vi.spyOn(button, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 100, height: 40 } as DOMRect);
  button.setAttribute("aria-disabled", "true"); fireEvent.click(button);
  expect(document.querySelector("[data-motion-press]")).toBeNull();
  button.removeAttribute("aria-disabled");
  fireEvent.click(button); act(() => { reduced.matches = true; change(); });
  expect(records[1]!.cancel).toHaveBeenCalledOnce();
  expect(document.querySelector("[data-motion-press]")).toBeNull();
  fireEvent.click(button); expect(document.querySelector("[data-motion-press]")).toBeNull();
  act(() => { reduced.matches = false; change(); });
  fireEvent.click(button);
  expect(document.querySelector("[data-motion-press]")).not.toBeNull();
  fireEvent(button.closest("section")!, new Event("scroll", { bubbles: false }));
  expect(document.querySelector("[data-motion-press]")).toBeNull();
  fireEvent.click(button);
  const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  fireEvent(document, new Event("visibilitychange"));
  expect(document.querySelector("[data-motion-press]")).toBeNull();
  hidden.mockRestore();
  fireEvent.click(button); fireEvent(window, new Event("blur"));
  expect(document.querySelector("[data-motion-press]")).toBeNull();
  fireEvent.click(button); view.rerender(<Page enabled={false} />);
  expect(document.querySelector("[data-motion-press]")).toBeNull();
  view.unmount();
});

it("reveals focused controls immediately instead of keeping their parent entrance animation", () => {
  const { records } = pressAnimations();
  const view = render(<Page />);
  act(() => screen.getByRole("button").focus());
  expect(records[0]!.cancel).toHaveBeenCalledOnce();
  view.unmount();
  expect(records[0]!.cancel).toHaveBeenCalledOnce();
});
