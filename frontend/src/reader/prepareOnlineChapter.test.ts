import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { openOnlineReader, registerOnlineReaderChapter, putReaderProgress } from "../api/client";
import { requestReaderPage } from "./readerPageRequests";
import { prepareOnlineChapter, warmOnlineChapter } from "./prepareOnlineChapter";

vi.mock("../api/client", () => ({openOnlineReader: vi.fn(), registerOnlineReaderChapter: vi.fn(), putReaderProgress: vi.fn()}));
vi.mock("./readerPageRequests", () => ({requestReaderPage: vi.fn()}));
const target = {seriesId: "series", connectorId: "yomira.metadata.mangadex", remoteSeriesId: "remote-series", remoteId: "remote-1"};
function client() { return new QueryClient({defaultOptions: {queries: {retry:false, gcTime:0}}}); }

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(registerOnlineReaderChapter).mockResolvedValue({chapterId:"chapter", seriesId:"series"});
  vi.mocked(openOnlineReader).mockResolvedValue({chapter: {chapterId:"chapter", seriesId:"series", displayLabel:"Chapter", pageCount:8},
    progress: {chapterId:"chapter", seriesId:"series", pageIndex:3, percentage:"42.86", updatedAt:"2026-10-17T12:00:00Z"}});
  vi.mocked(requestReaderPage).mockResolvedValue(new Blob(["image"]));
});

describe("safe chapter preparation", () => {
  it("prepares only the resume image and reuses registration for the actual reading action", async () => {
    const queries = client();
    await warmOnlineChapter(queries, target);
    expect(requestReaderPage).toHaveBeenCalledWith("chapter", 4, expect.any(AbortSignal), "preload");
    await prepareOnlineChapter(queries, target);
    await warmOnlineChapter(queries, target);
    expect(registerOnlineReaderChapter).toHaveBeenCalledTimes(1);
    expect(openOnlineReader).toHaveBeenCalledTimes(1);
    expect(openOnlineReader).toHaveBeenCalledWith("chapter", false, true);
    expect(requestReaderPage).toHaveBeenCalledTimes(1);
    expect(putReaderProgress).not.toHaveBeenCalled();
    queries.clear();
  });

  it("does not open the native engine speculatively", async () => {
    await warmOnlineChapter(client(), {...target, connectorId:"yomira.fmd2.webtoons"});
    expect(registerOnlineReaderChapter).not.toHaveBeenCalled();
    expect(openOnlineReader).not.toHaveBeenCalled();
  });

  it("allows only one speculative chapter at a time", async () => {
    const queries = client();
    let complete!: (value: Awaited<ReturnType<typeof openOnlineReader>>) => void;
    const opened = await openOnlineReader("chapter");
    vi.mocked(openOnlineReader).mockClear().mockImplementationOnce(() => new Promise((resolve) => {complete = resolve;}));
    const first = warmOnlineChapter(queries, target);
    await vi.waitFor(() => expect(openOnlineReader).toHaveBeenCalledTimes(1));
    await warmOnlineChapter(queries, {...target, remoteId:"remote-2"});
    expect(registerOnlineReaderChapter).toHaveBeenCalledTimes(1);
    complete(opened);
    await first;
    queries.clear();
  });

  it("rejects a different series before loading its images", async () => {
    const queries = client();
    vi.mocked(registerOnlineReaderChapter).mockResolvedValueOnce({chapterId:"wrong", seriesId:"other"});
    await expect(warmOnlineChapter(queries, target)).rejects.toThrow("La fuente cambió la edición");
    expect(openOnlineReader).not.toHaveBeenCalled();
    expect(requestReaderPage).not.toHaveBeenCalled();
    await expect(prepareOnlineChapter(queries, target)).resolves.toEqual({chapterId:"chapter", seriesId:"series"});
    queries.clear();
  });
});
