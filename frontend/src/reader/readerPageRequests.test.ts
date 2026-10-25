import { afterEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { getReaderPageBlob } from "../api/client";
import { requestReaderPage } from "./readerPageRequests";

vi.mock("../api/client", () => ({getReaderPageBlob: vi.fn()}));
afterEach(() => vi.clearAllMocks());

describe("reader image priority", () => {
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
    await waitFor(() => expect(releases.has(3)).toBe(true));
    releases.get(2)!(new Blob(["two"]));
    releases.get(3)!(new Blob(["three"]));
    await Promise.all([second, background]);
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
