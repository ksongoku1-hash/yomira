import { afterEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { getReaderPageBlob } from "../api/client";
import { prioritizeReaderPage, requestReaderPage } from "./readerPageRequests";

vi.mock("../api/client", () => ({getReaderPageBlob: vi.fn()}));
afterEach(() => vi.clearAllMocks());

describe("reader image priority", () => {
  it("promotes the queued preload that becomes visible without fetching it twice", async () => {
    const releases = new Map<number, (blob: Blob) => void>();
    vi.mocked(getReaderPageBlob).mockImplementation((_chapter, page) => new Promise((resolve) => releases.set(page, resolve)));
    const firstPreload = requestReaderPage("chapter", 1, undefined, "preload");
    const controller = new AbortController();
    const next = requestReaderPage("chapter", 2, controller.signal, "preload");
    expect(releases.has(2)).toBe(false);
    prioritizeReaderPage(controller.signal);
    expect(vi.mocked(getReaderPageBlob).mock.calls.map(([, page]) => page)).toEqual([1, 2]);
    prioritizeReaderPage(controller.signal);
    expect(getReaderPageBlob).toHaveBeenCalledTimes(2);
    expect(getReaderPageBlob).toHaveBeenLastCalledWith("chapter", 2, controller.signal);
    releases.get(1)!(new Blob(["preload"]));
    releases.get(2)!(new Blob(["visible"]));
    await Promise.all([firstPreload, next]);
    await Promise.resolve();
  });

  it("keeps a slot for the visible page while slow preloads are queued", async () => {
    const releases = new Map<number, (blob: Blob) => void>();
    vi.mocked(getReaderPageBlob).mockImplementation((_chapter, page) => new Promise((resolve) => releases.set(page, resolve)));
    const firstPreload = requestReaderPage("chapter", 1, undefined, "preload");
    const secondPreload = requestReaderPage("chapter", 2, undefined, "preload");
    expect(vi.mocked(getReaderPageBlob).mock.calls.map(([, page]) => page)).toEqual([1]);
    const visible = requestReaderPage("chapter", 3);
    expect(vi.mocked(getReaderPageBlob).mock.calls.map(([, page]) => page)).toEqual([1, 3]);
    releases.get(3)!(new Blob(["visible"]));
    await visible;
    expect(releases.has(2)).toBe(false);
    releases.get(1)!(new Blob(["first preload"]));
    await firstPreload;
    await waitFor(() => expect(releases.has(2)).toBe(true));
    releases.get(2)!(new Blob(["second preload"]));
    await secondPreload;
    await Promise.resolve();
  });

  it("releases the preload slot after a failed image without blocking later work", async () => {
    let fail!: (error: Error) => void;
    const releases = new Map<number, (blob: Blob) => void>();
    vi.mocked(getReaderPageBlob).mockImplementation((_chapter, page) => page === 1
      ? new Promise((_resolve, reject) => { fail = reject; })
      : new Promise((resolve) => releases.set(page, resolve)));
    const first = requestReaderPage("chapter", 1, undefined, "preload");
    const failed = expect(first).rejects.toThrow("source unavailable");
    const next = requestReaderPage("chapter", 2, undefined, "preload");
    fail(new Error("source unavailable"));
    await failed;
    await waitFor(() => expect(releases.has(2)).toBe(true));
    releases.get(2)!(new Blob(["page"]));
    await next;
    await Promise.resolve();
  });

  it("runs at most two requests and puts visible pages before waiting preloads", async () => {
    const releases = new Map<number, (blob: Blob) => void>();
    vi.mocked(getReaderPageBlob).mockImplementation((_chapter, page) => new Promise((resolve) => releases.set(page, resolve)));
    const first = requestReaderPage("chapter", 1);
    const second = requestReaderPage("chapter", 2, undefined, "preload");
    const background = requestReaderPage("chapter", 3, undefined, "preload");
    const visible = requestReaderPage("chapter", 4);
    expect(vi.mocked(getReaderPageBlob).mock.calls.map(([, page]) => page)).toEqual([1, 2]);
    releases.get(1)!(new Blob(["one"]));
    await first;
    await waitFor(() => expect(releases.has(4)).toBe(true));
    expect(releases.has(3)).toBe(false);
    releases.get(4)!(new Blob(["four"]));
    await visible;
    expect(releases.has(3)).toBe(false);
    releases.get(2)!(new Blob(["two"]));
    await second;
    await waitFor(() => expect(releases.has(3)).toBe(true));
    releases.get(3)!(new Blob(["three"]));
    await background;
    await Promise.resolve();
  });

  it("drops an aborted waiting page without sending it to the server", async () => {
    const releases: Array<(blob: Blob) => void> = [];
    vi.mocked(getReaderPageBlob).mockImplementation(() => new Promise((resolve) => releases.push(resolve)));
    const first = requestReaderPage("chapter", 1);
    const second = requestReaderPage("chapter", 2);
    const controller = new AbortController();
    const waiting = requestReaderPage("chapter", 3, controller.signal, "preload");
    controller.abort();
    await expect(waiting).rejects.toMatchObject({name: "AbortError"});
    prioritizeReaderPage(controller.signal);
    releases.forEach((release) => release(new Blob(["page"])));
    await Promise.all([first, second]);
    await Promise.resolve();
    expect(getReaderPageBlob).toHaveBeenCalledTimes(2);
  });

  it("does not send a request whose signal was already aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(requestReaderPage("chapter", 1, controller.signal)).rejects.toMatchObject({name: "AbortError"});
    expect(getReaderPageBlob).not.toHaveBeenCalled();
  });
});
