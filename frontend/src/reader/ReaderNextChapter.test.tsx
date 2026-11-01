import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReaderNextChapter } from "./ReaderNextChapter";

const next = { seriesId: "work", connectorId: "source", remoteSeriesId: "work", remoteId: "2", displayLabel: "Capítulo 2" };
function setup(automatic = true) {
  const onOpen = vi.fn(); const onCancel = vi.fn();
  const view = render(<MemoryRouter><ReaderNextChapter next={next} automatic={automatic} disabled={false} onOpen={onOpen} onCancel={onCancel} /></MemoryRouter>);
  return { ...view, onOpen, onCancel };
}
afterEach(() => vi.useRealTimers());
describe("next chapter countdown", () => {
  it("fills the button over exactly five seconds and opens only once", () => {
    vi.useFakeTimers(); const { onOpen } = setup();
    const button = screen.getByRole("button", { name: /Siguiente capítulo/ });
    expect(screen.getByRole("status")).toHaveTextContent("en 5 segundos");
    expect(button.style.getPropertyValue("--advance-progress")).toBe("0");
    act(() => vi.advanceTimersByTime(2500));
    expect(button.style.getPropertyValue("--advance-progress")).toBe("0.5");
    expect(screen.getByRole("status")).toHaveTextContent("en 3 segundos");
    act(() => vi.advanceTimersByTime(2400)); expect(onOpen).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(100)); expect(onOpen).toHaveBeenCalledExactlyOnceWith(next);
    act(() => vi.advanceTimersByTime(10000)); expect(onOpen).toHaveBeenCalledTimes(1);
  });
  it("opens immediately on click without leaving a second automatic opening", () => {
    vi.useFakeTimers(); const { onOpen } = setup();
    act(() => vi.advanceTimersByTime(2000));
    fireEvent.click(screen.getByRole("button", { name: /Siguiente capítulo/ }));
    act(() => vi.advanceTimersByTime(10000));
    expect(onOpen).toHaveBeenCalledExactlyOnceWith(next);
  });
  it("cancels and starts a fresh five seconds after leaving and reaching the end again", () => {
    vi.useFakeTimers(); const { onOpen, onCancel, rerender } = setup();
    act(() => vi.advanceTimersByTime(2000));
    rerender(<MemoryRouter><ReaderNextChapter next={next} automatic={false} disabled={false} onOpen={onOpen} onCancel={onCancel} /></MemoryRouter>);
    act(() => vi.advanceTimersByTime(6000)); expect(onOpen).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    rerender(<MemoryRouter><ReaderNextChapter next={next} automatic disabled={false} onOpen={onOpen} onCancel={onCancel} /></MemoryRouter>);
    expect(screen.getByRole("status")).toHaveTextContent("en 5 segundos");
    act(() => vi.advanceTimersByTime(4900)); expect(onOpen).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(100)); expect(onOpen).toHaveBeenCalledTimes(1);
  });
  it("stops the timer on cancellation and on unmount", () => {
    vi.useFakeTimers(); const { onOpen, onCancel, unmount } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Cancelar avance automático" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(6000)); expect(onOpen).not.toHaveBeenCalled();
    unmount(); expect(vi.getTimerCount()).toBe(0);
  });
  it("keeps local chapter links and remains manual when automatic advance is disabled", () => {
    vi.useFakeTimers(); const onOpen = vi.fn();
    render(<MemoryRouter><ReaderNextChapter next={{ chapterId: "chapter", seriesId: "work", displayLabel: "Capítulo 2", pageCount: 3 }} automatic={false} disabled={false} onOpen={onOpen} onCancel={vi.fn()} /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "Siguiente capítulo · Capítulo 2" })).toHaveAttribute("href", "/reader/chapter");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(10000)); expect(onOpen).not.toHaveBeenCalled();
  });
  it("clears an active countdown when leaving the reader", () => {
    vi.useFakeTimers(); const { onOpen, unmount } = setup();
    act(() => vi.advanceTimersByTime(2000));
    unmount(); expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(6000)); expect(onOpen).not.toHaveBeenCalled();
  });
  it("restarts for a changed destination and never opens the previous target", () => {
    vi.useFakeTimers(); const { onOpen, onCancel, rerender } = setup();
    act(() => vi.advanceTimersByTime(4000));
    const changed = { ...next, remoteId: "3", displayLabel: "Capítulo 3" };
    rerender(<MemoryRouter><ReaderNextChapter next={changed} automatic disabled={false} onOpen={onOpen} onCancel={onCancel} /></MemoryRouter>);
    act(() => vi.advanceTimersByTime(1000)); expect(onOpen).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(4000)); expect(onOpen).toHaveBeenCalledExactlyOnceWith(changed);
  });
});
