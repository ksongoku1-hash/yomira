import type { QueryClient } from "@tanstack/react-query";
import { openOnlineReader, registerOnlineReaderChapter } from "../api/client";
import { requestReaderPage } from "./readerPageRequests";

interface Target {
  seriesId: string;
  connectorId: string;
  remoteSeriesId: string;
  remoteId: string;
}
const warming = new WeakSet<QueryClient>();

export function prepareOnlineChapter(client: QueryClient, target: Target) {
  return client.fetchQuery({
    queryKey: ["online-reader-registration", target.connectorId, target.remoteSeriesId, target.remoteId],
    queryFn: async () => {
      const registered = await registerOnlineReaderChapter(target.connectorId, target.remoteSeriesId, target.remoteId);
      if (registered.seriesId !== target.seriesId) throw new Error("La fuente cambió la edición vinculada. Vuelve a abrir la ficha.");
      return registered;
    },
    staleTime: 300_000, retry: false,
  });
}

export async function warmOnlineChapter(client: QueryClient, target: Target): Promise<void> {
  if (target.connectorId !== "yomira.metadata.mangadex" || warming.has(client)) return;
  warming.add(client);
  try {
    const registered = await prepareOnlineChapter(client, target);
    await client.prefetchQuery({
      queryKey: ["reader-warm-image", registered.chapterId], staleTime: 60_000, retry: false,
      queryFn: async ({ signal }) => {
        const opened = await openOnlineReader(registered.chapterId, false, true);
        if (opened.chapter.seriesId !== target.seriesId) throw new Error("La fuente cambió la serie.");
        const page = Math.min(opened.chapter.pageCount, (opened.progress?.pageIndex ?? 0) + 1);
        await requestReaderPage(registered.chapterId, page, signal, "preload");
        return true;
      },
    });
  } finally {
    warming.delete(client);
  }
}
