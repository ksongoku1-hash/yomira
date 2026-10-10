import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import {
  ApiError,
  getReaderChapter,
  getReaderPageBlob,
  getReaderProgress,
  getReaderSettings,
  putReaderProgress,
  putReaderSettings,
} from "../api/client";
import type { ReaderSettings } from "../api/client";
import { ReaderPageCache } from "./readerPageCache";
import type { CachedReaderPage, EvictedReaderPage } from "./readerPageCache";
import styles from "./ReaderPage.module.css";

type ReaderMode = ReaderSettings["mode"];
type ReaderFit = "width" | "height" | null;
type ReaderSettingsSaveStatus = "idle" | "saving" | "saved" | "error";

const UUID_V4_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function originatingSeriesIdFromState(state: unknown): string | null {
  if (typeof state !== "object" || state === null || !("originatingSeriesId" in state)) {
    return null;
  }
  const seriesId = state.originatingSeriesId;
  return typeof seriesId === "string" && UUID_V4_PATTERN.test(seriesId) ? seriesId : null;
}

interface ReaderVisiblePageRange {
  chapterId: string;
  firstPage: number;
  lastPage: number;
}

interface ReaderTouchStart {
  pointerId: number;
  x: number;
  y: number;
  startedAt: number;
}

type ReaderPageResource =
  | { chapterId: string; kind: "loading"; controller: AbortController }
  | CachedReaderPage
  | { chapterId: string; kind: "error"; error: unknown };

type ReaderProgressSaveView =
  | { chapterId: string; status: "idle" }
  | { chapterId: string; status: "saving" | "saved"; pageIndex: number }
  | { chapterId: string; status: "error"; pageIndex: number; error: unknown };

interface PendingProgressSave {
  generation: number;
  pageIndex: number;
}

interface ChapterProgressSaveQueue {
  active: PendingProgressSave | null;
  pending: PendingProgressSave | null;
  latestGeneration: number;
  view: ReaderProgressSaveView;
  listeners: Set<(view: ReaderProgressSaveView) => void>;
}

const chapterProgressSaveQueues = new Map<string, ChapterProgressSaveQueue>();

function progressQueueFor(chapterId: string): ChapterProgressSaveQueue {
  let queue = chapterProgressSaveQueues.get(chapterId);
  if (queue === undefined) {
    queue = {
      active: null,
      pending: null,
      latestGeneration: 0,
      view: { chapterId, status: "idle" },
      listeners: new Set(),
    };
    chapterProgressSaveQueues.set(chapterId, queue);
  }
  return queue;
}

function publishProgressQueueView(
  chapterId: string,
  queue: ChapterProgressSaveQueue,
  view: ReaderProgressSaveView,
): void {
  queue.view = view;
  for (const listener of [...queue.listeners]) listener(view);
  if (
    queue.active === null &&
    queue.pending === null &&
    queue.listeners.size === 0 &&
    chapterProgressSaveQueues.get(chapterId) === queue
  ) {
    chapterProgressSaveQueues.delete(chapterId);
  }
}

function hasPendingProgressSave(chapterId: string): boolean {
  const queue = chapterProgressSaveQueues.get(chapterId);
  return queue !== undefined && (queue.active !== null || queue.pending !== null);
}

function subscribeToProgressSaves(
  chapterId: string,
  listener: (view: ReaderProgressSaveView) => void,
): () => void {
  const queue = progressQueueFor(chapterId);
  queue.listeners.add(listener);
  listener(queue.view);
  return () => {
    queue.listeners.delete(listener);
    if (
      queue.active === null &&
      queue.pending === null &&
      queue.listeners.size === 0 &&
      chapterProgressSaveQueues.get(chapterId) === queue
    ) {
      chapterProgressSaveQueues.delete(chapterId);
    }
  };
}

function runChapterProgressSaveQueue(chapterId: string): void {
  const queue = chapterProgressSaveQueues.get(chapterId);
  if (queue === undefined || queue.active !== null || queue.pending === null) return;

  const operation = queue.pending;
  queue.pending = null;
  queue.active = operation;

  void putReaderProgress(chapterId, operation.pageIndex)
    .then(() => {
      if (queue.latestGeneration !== operation.generation || queue.pending !== null) return;
      publishProgressQueueView(chapterId, queue, {
        chapterId,
        status: "saved",
        pageIndex: operation.pageIndex,
      });
    })
    .catch((error: unknown) => {
      if (queue.latestGeneration !== operation.generation || queue.pending !== null) return;
      publishProgressQueueView(chapterId, queue, {
        chapterId,
        status: "error",
        pageIndex: operation.pageIndex,
        error,
      });
    })
    .finally(() => {
      queue.active = null;
      if (queue.pending !== null) {
        publishProgressQueueView(chapterId, queue, {
          chapterId,
          status: "saving",
          pageIndex: queue.pending.pageIndex,
        });
        runChapterProgressSaveQueue(chapterId);
      } else if (
        queue.listeners.size === 0 &&
        chapterProgressSaveQueues.get(chapterId) === queue
      ) {
        chapterProgressSaveQueues.delete(chapterId);
      }
    });
}

function enqueueChapterProgressSave(chapterId: string, pageIndex: number): void {
  const queue = progressQueueFor(chapterId);
  const operation = {
    generation: queue.latestGeneration + 1,
    pageIndex,
  };
  queue.latestGeneration = operation.generation;
  queue.pending = operation;
  publishProgressQueueView(chapterId, queue, {
    chapterId,
    status: "saving",
    pageIndex,
  });
  runChapterProgressSaveQueue(chapterId);
}

const VERTICAL_WINDOW_BEHIND = 1;
const MAX_VERTICAL_PRELOAD_AHEAD = 2;
const DEFAULT_VERTICAL_PAGE_HEIGHT = 768;
const DEFAULT_READER_SETTINGS: ReaderSettings = {
  cache_limit_mb: 500,
  preload_ahead: MAX_VERTICAL_PRELOAD_AHEAD,
  mode: "single",
  rtl: false,
  e_reader: false,
};
const READER_SETTINGS_QUERY_KEY = ["reader-settings"] as const;

let readerSettingsWriteTail: Promise<void> = Promise.resolve();

function putReaderSettingsInOrder(settings: ReaderSettings): Promise<ReaderSettings> {
  const operation = readerSettingsWriteTail
    .catch(() => undefined)
    .then(() => putReaderSettings(settings));
  readerSettingsWriteTail = operation.then(() => undefined, () => undefined);
  return operation;
}

function errorDetails(error: unknown): {
  message: string;
  code: string;
  traceId: string | null;
} {
  if (!(error instanceof ApiError)) {
    return {
      message: "Falló una operación del lector.",
      code: "Sin código de servidor",
      traceId: null,
    };
  }
  return {
    message: error.message,
    code: error.code ?? "Sin código de servidor",
    traceId: error.traceId,
  };
}

function releasePageResource(resource: ReaderPageResource): void {
  if (resource.kind === "loading") {
    resource.controller.abort();
  } else if (resource.kind === "ready") {
    URL.revokeObjectURL(resource.url);
  }
}

function releaseEvictedReaderPages(
  evicted: readonly EvictedReaderPage[],
  activeResources: Map<number, ReaderPageResource>,
  suppressedPreloadPages: Set<number>,
): boolean {
  let changed = false;
  for (const entry of evicted) {
    if (activeResources.get(entry.pageNumber) === entry.resource) {
      activeResources.delete(entry.pageNumber);
      suppressedPreloadPages.add(entry.pageNumber);
      changed = true;
    }
    URL.revokeObjectURL(entry.resource.url);
  }
  return changed;
}

function clearReaderPageResources(
  cache: ReaderPageCache,
  activeResources: Map<number, ReaderPageResource>,
): boolean {
  const cachedEntries = cache.clear();
  const cachedResources = new Set(cachedEntries.map((entry) => entry.resource));
  for (const entry of cachedEntries) URL.revokeObjectURL(entry.resource.url);
  const hadActiveResources = activeResources.size > 0;
  for (const resource of activeResources.values()) {
    if (resource.kind === "loading") releasePageResource(resource);
    else if (resource.kind === "ready" && !cachedResources.has(resource)) {
      releasePageResource(resource);
    }
  }
  activeResources.clear();
  return cachedResources.size > 0 || hadActiveResources;
}

function initialVerticalPageHeight(): number {
  if (typeof window === "undefined") return DEFAULT_VERTICAL_PAGE_HEIGHT;
  return Math.max(320, Math.min(Math.floor(window.innerHeight * 0.8), 864));
}

function visiblePageRangeForViewport(
  chapterId: string,
  scrollTop: number,
  viewportHeight: number,
  pageStep: number,
  pageCount: number,
): ReaderVisiblePageRange | null {
  if (viewportHeight <= 0 || pageCount < 1) return null;
  const safePageStep = Math.max(1, pageStep);
  const safeScrollTop = Math.max(0, scrollTop);
  const firstPage = Math.min(
    pageCount,
    Math.max(1, Math.floor(safeScrollTop / safePageStep) + 1),
  );
  const lastPage = Math.min(
    pageCount,
    Math.max(1, Math.floor((safeScrollTop + viewportHeight - 1) / safePageStep) + 1),
  );
  return { chapterId, firstPage, lastPage };
}

function sameVisiblePageRange(
  left: ReaderVisiblePageRange | null,
  right: ReaderVisiblePageRange | null,
): boolean {
  return left === right || (
    left !== null &&
    right !== null &&
    left.chapterId === right.chapterId &&
    left.firstPage === right.firstPage &&
    left.lastPage === right.lastPage
  );
}

function verticalWindowLastPage(
  page: number,
  pageCount: number,
  preloadAhead: number,
  chapterId: string | undefined,
  visiblePageRange: ReaderVisiblePageRange | null,
): number {
  const configuredLastPage = page + Math.min(preloadAhead, MAX_VERTICAL_PRELOAD_AHEAD);
  const visibleLastPage =
    chapterId !== undefined &&
    visiblePageRange?.chapterId === chapterId &&
    visiblePageRange.firstPage === page
      ? Math.min(visiblePageRange.lastPage, page + MAX_VERTICAL_PRELOAD_AHEAD)
      : page;
  return Math.min(pageCount, Math.max(configuredLastPage, visibleLastPage));
}

function pageImageClass(vertical: boolean, fit: ReaderFit): string {
  return [
    vertical ? styles.verticalPageImage : styles.pageImage,
    fit === "width" ? styles.fitWidthImage : "",
    fit === "height" ? styles.fitHeightImage : "",
  ].filter(Boolean).join(" ");
}

export function ReaderPage() {
  const location = useLocation();
  const { chapterId = "" } = useParams();
  const [page, setPage] = useState(1);
  const [readerSettingsDraft, setReaderSettingsDraft] = useState<ReaderSettings | null>(null);
  const [settingsSaveStatus, setSettingsSaveStatus] = useState<ReaderSettingsSaveStatus>("idle");
  const [settingsSaveError, setSettingsSaveError] = useState<unknown>(null);
  const [zoom, setZoom] = useState(1);
  const [fit, setFit] = useState<ReaderFit>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [fullscreenMessage, setFullscreenMessage] = useState("");
  const [progressSaveView, setProgressSaveView] = useState<ReaderProgressSaveView>({
    chapterId,
    status: "idle",
  });
  const [retryCount, setRetryCount] = useState(0);
  const [pageResources, setPageResources] = useState<ReadonlyMap<number, ReaderPageResource>>(
    () => new Map(),
  );
  const [visiblePageRange, setVisiblePageRange] = useState<ReaderVisiblePageRange | null>(null);
  const [verticalPageHeight, setVerticalPageHeight] = useState(initialVerticalPageHeight);
  const pageResourcesRef = useRef(new Map<number, ReaderPageResource>());
  const pageCacheRef = useRef(new ReaderPageCache(DEFAULT_READER_SETTINGS.cache_limit_mb * 1_000_000));
  const suppressedPreloadPagesRef = useRef(new Set<number>());
  const cacheChapterIdRef = useRef<string | null>(null);
  const touchStartRef = useRef<ReaderTouchStart | null>(null);
  const verticalViewportRef = useRef<HTMLDivElement | null>(null);
  const readerRootRef = useRef<HTMLElement | null>(null);
  const pendingScrollPageRef = useRef<number | null>(null);
  const pageRef = useRef(1);
  const routeChapterIdRef = useRef(chapterId);
  const settingsWriteGenerationRef = useRef(0);
  const restoredProgressChapterRef = useRef<string | null>(null);
  const initialProgressSavedChapterRef = useRef<string | null>(null);
  const interactedChapterRef = useRef<string | null>(null);
  const queryClient = useQueryClient();
  const chapterQuery = useQuery({
    queryKey: ["reader-chapter", chapterId],
    queryFn: () => getReaderChapter(chapterId),
    enabled: chapterId.length > 0,
  });
  const chapter = chapterQuery.data;
  const returnSeriesId = chapter?.seriesId ?? originatingSeriesIdFromState(location.state);
  const readerReturnHref = returnSeriesId === null ? "/downloads" : `/series/${returnSeriesId}`;
  const readerReturnLabel = returnSeriesId === null
    ? "← Volver a descargas"
    : "← Volver a la serie";
  const readerChapterId = chapter?.chapterId;
  const readerSettingsQuery = useQuery({
    queryKey: READER_SETTINGS_QUERY_KEY,
    queryFn: getReaderSettings,
    retry: false,
  });
  const readerSettings = readerSettingsDraft ?? readerSettingsQuery.data ?? DEFAULT_READER_SETTINGS;
  const mode = readerSettings.mode;
  const rtl = readerSettings.rtl;
  const eReader = readerSettings.e_reader;
  const settingsAvailable = readerSettingsQuery.isSuccess;
  const effectivePreloadAhead = eReader ? 0 : readerSettings.preload_ahead;
  const verticalPageStep = verticalPageHeight * Math.max(1, zoom);
  const refreshVisiblePageRange = useCallback((scrollTop: number, viewportHeight: number) => {
    const nextRange =
      mode === "vertical" && chapter !== undefined && readerChapterId !== undefined
        ? visiblePageRangeForViewport(
          readerChapterId,
          scrollTop,
          viewportHeight,
          verticalPageStep,
          chapter.pageCount,
        )
        : null;
    setVisiblePageRange((currentRange) =>
      sameVisiblePageRange(currentRange, nextRange) ? currentRange : nextRange,
    );
  }, [chapter?.pageCount, mode, readerChapterId, verticalPageStep]);
  const progressQuery = useQuery({
    queryKey: ["reader-progress", readerChapterId ?? chapterId],
    queryFn: () => readerChapterId === undefined
      ? Promise.resolve(null)
      : getReaderProgress(readerChapterId),
    enabled: readerChapterId !== undefined,
    retry: false,
  });

  const getPinnedPageNumbers = (): Set<number> => {
    if (chapter === undefined || readerChapterId === undefined) return new Set();
    if (mode === "double") {
      return new Set([page, ...(page + 1 <= chapter.pageCount ? [page + 1] : [])]);
    }
    if (mode === "vertical") {
      const pinned = new Set([page]);
      const viewport = verticalViewportRef.current;
      if (viewport === null || viewport.clientHeight <= 0) return pinned;
      const pageStep = Math.max(1, verticalPageStep);
      const firstVisible = Math.max(1, Math.floor(viewport.scrollTop / pageStep) + 1);
      const lastVisible = Math.min(
        chapter.pageCount,
        Math.floor((viewport.scrollTop + viewport.clientHeight - 1) / pageStep) + 1,
      );
      for (let pageNumber = firstVisible; pageNumber <= lastVisible; pageNumber += 1) {
        pinned.add(pageNumber);
      }
      return pinned;
    }
    return new Set([page]);
  };

  const updateReaderSettings = (updates: Partial<ReaderSettings>) => {
    if (!settingsAvailable) return;
    const nextSettings: ReaderSettings = { ...readerSettings, ...updates };
    const generation = settingsWriteGenerationRef.current + 1;
    settingsWriteGenerationRef.current = generation;
    setReaderSettingsDraft(nextSettings);
    setSettingsSaveStatus("saving");
    setSettingsSaveError(null);
    void putReaderSettingsInOrder(nextSettings)
      .then((savedSettings) => {
        if (settingsWriteGenerationRef.current !== generation) return;
        queryClient.setQueryData(READER_SETTINGS_QUERY_KEY, savedSettings);
        setReaderSettingsDraft(null);
        setSettingsSaveStatus("saved");
      })
      .catch((error: unknown) => {
        if (settingsWriteGenerationRef.current !== generation) return;
        setSettingsSaveError(error);
        setSettingsSaveStatus("error");
      });
  };

  const updatePagePosition = (nextPage: number, userInitiated: boolean) => {
    if (pageRef.current === nextPage) return;
    pageRef.current = nextPage;
    setPage(nextPage);
    if (userInitiated && readerChapterId !== undefined) {
      interactedChapterRef.current = readerChapterId;
      enqueueChapterProgressSave(readerChapterId, nextPage - 1);
    }
  };

  useEffect(() => {
    if (routeChapterIdRef.current === chapterId) return;
    routeChapterIdRef.current = chapterId;
    setPage(1);
    pageRef.current = 1;
    pendingScrollPageRef.current = 1;
    restoredProgressChapterRef.current = null;
    initialProgressSavedChapterRef.current = null;
    interactedChapterRef.current = null;
    setZoom(1);
    setFit(null);
    setProgressSaveView({ chapterId, status: "idle" });
  }, [chapterId]);

  useEffect(() => {
    if (readerChapterId === undefined) return;
    let subscribed = true;
    const unsubscribe = subscribeToProgressSaves(readerChapterId, (view) => {
      if (!subscribed) return;
      setProgressSaveView(view);
      if (view.status === "saving" && chapter !== undefined) {
        const queuedPage = Math.min(chapter.pageCount, view.pageIndex + 1);
        if (pageRef.current !== queuedPage) {
          pageRef.current = queuedPage;
          pendingScrollPageRef.current = queuedPage;
          setPage(queuedPage);
        }
      }
      if (view.status === "saved") {
        void queryClient.invalidateQueries({
          queryKey: ["reader-progress", readerChapterId],
        });
      }
    });
    return () => {
      subscribed = false;
      unsubscribe();
    };
  }, [chapter, queryClient, readerChapterId]);

  useEffect(() => {
    if (
      chapter === undefined ||
      readerChapterId === undefined ||
      !progressQuery.isSuccess ||
      progressQuery.fetchStatus === "fetching" ||
      hasPendingProgressSave(readerChapterId) ||
      restoredProgressChapterRef.current === readerChapterId
    ) return;

    restoredProgressChapterRef.current = readerChapterId;
    if (interactedChapterRef.current === readerChapterId) return;
    const restoredPage = progressQuery.data === null
      ? 1
      : Math.min(chapter.pageCount, progressQuery.data.pageIndex + 1);
    updatePagePosition(restoredPage, false);
  }, [
    chapter,
    progressQuery.data,
    progressQuery.fetchStatus,
    progressQuery.isSuccess,
    readerChapterId,
  ]);

  useEffect(() => {
    if (
      chapter === undefined ||
      readerChapterId === undefined ||
      !progressQuery.isSuccess ||
      progressQuery.fetchStatus === "fetching" ||
      progressQuery.data !== null ||
      restoredProgressChapterRef.current !== readerChapterId ||
      interactedChapterRef.current === readerChapterId ||
      initialProgressSavedChapterRef.current === readerChapterId
    ) return;

    const initialPage = pageResources.get(page);
    if (
      initialPage?.chapterId !== readerChapterId ||
      initialPage.kind !== "ready" ||
      !initialPage.decoded
    ) return;

    initialProgressSavedChapterRef.current = readerChapterId;
    enqueueChapterProgressSave(readerChapterId, page - 1);
  }, [
    chapter,
    page,
    pageResources,
    progressQuery.data,
    progressQuery.fetchStatus,
    progressQuery.isSuccess,
    readerChapterId,
  ]);

  useEffect(() => {
    const updatePageHeight = () => {
      pendingScrollPageRef.current = pageRef.current;
      setVerticalPageHeight(initialVerticalPageHeight());
    };
    window.addEventListener("resize", updatePageHeight);
    return () => window.removeEventListener("resize", updatePageHeight);
  }, []);

  useEffect(() => {
    const updateFullscreenState = () => {
      setIsFullscreen(document.fullscreenElement === readerRootRef.current);
    };
    document.addEventListener("fullscreenchange", updateFullscreenState);
    return () => document.removeEventListener("fullscreenchange", updateFullscreenState);
  }, []);

  useEffect(() => {
    const viewport = verticalViewportRef.current;
    if (mode !== "vertical" || viewport === null) {
      refreshVisiblePageRange(0, 0);
      return;
    }
    refreshVisiblePageRange(viewport.scrollTop, viewport.clientHeight);
  }, [
    chapter?.pageCount,
    isFullscreen,
    mode,
    page,
    readerChapterId,
    refreshVisiblePageRange,
    verticalPageStep,
  ]);

  useEffect(() => {
    if (readerChapterId === undefined || chapter === undefined) {
      const changed = clearReaderPageResources(pageCacheRef.current, pageResourcesRef.current);
      suppressedPreloadPagesRef.current.clear();
      cacheChapterIdRef.current = null;
      if (changed) setPageResources(new Map());
      return;
    }

    let changed = false;
    if (cacheChapterIdRef.current !== readerChapterId) {
      changed = clearReaderPageResources(pageCacheRef.current, pageResourcesRef.current);
      suppressedPreloadPagesRef.current.clear();
      cacheChapterIdRef.current = readerChapterId;
    }
    changed = releaseEvictedReaderPages(
      pageCacheRef.current.setLimit(
        readerSettings.cache_limit_mb * 1_000_000,
        getPinnedPageNumbers(),
      ),
      pageResourcesRef.current,
      suppressedPreloadPagesRef.current,
    ) || changed;

    const firstPage = mode === "vertical"
      ? Math.max(1, page - VERTICAL_WINDOW_BEHIND)
      : page;
    const lastPage = mode === "vertical"
      ? verticalWindowLastPage(
        page,
        chapter.pageCount,
        effectivePreloadAhead,
        readerChapterId,
        visiblePageRange,
      )
      : mode === "double"
        ? Math.min(chapter.pageCount, page + 1)
      : page;
    const desiredPages = new Set<number>();
    for (let pageNumber = firstPage; pageNumber <= lastPage; pageNumber += 1) {
      desiredPages.add(pageNumber);
    }

    const currentOrVisiblePages = new Set([page]);
    if (
      mode === "vertical" &&
      visiblePageRange?.chapterId === readerChapterId
    ) {
      for (
        let pageNumber = visiblePageRange.firstPage;
        pageNumber <= visiblePageRange.lastPage;
        pageNumber += 1
      ) {
        currentOrVisiblePages.add(pageNumber);
      }
    }
    for (const pageNumber of suppressedPreloadPagesRef.current) {
      if (!desiredPages.has(pageNumber) || currentOrVisiblePages.has(pageNumber)) {
        suppressedPreloadPagesRef.current.delete(pageNumber);
      }
    }

    for (const [pageNumber, resource] of pageResourcesRef.current) {
      if (!desiredPages.has(pageNumber) || resource.chapterId !== readerChapterId) {
        if (resource.kind === "loading") resource.controller.abort();
        pageResourcesRef.current.delete(pageNumber);
        changed = true;
      }
    }

    for (const pageNumber of desiredPages) {
      if (suppressedPreloadPagesRef.current.has(pageNumber)) continue;
      const activeResource = pageResourcesRef.current.get(pageNumber);
      if (activeResource !== undefined) {
        if (activeResource.kind === "ready") pageCacheRef.current.get(pageNumber);
        continue;
      }
      const cachedResource = pageCacheRef.current.get(pageNumber);
      if (cachedResource !== undefined && cachedResource.chapterId === readerChapterId) {
        pageResourcesRef.current.set(pageNumber, cachedResource);
        changed = true;
        continue;
      }
      const pendingResource: ReaderPageResource = {
        chapterId: readerChapterId,
        kind: "loading",
        controller: new AbortController(),
      };
      pageResourcesRef.current.set(pageNumber, pendingResource);
      changed = true;

      void getReaderPageBlob(
        readerChapterId,
        pageNumber,
        pendingResource.controller.signal,
      )
        .then((blob) => {
          if (
            pendingResource.controller.signal.aborted ||
            pageResourcesRef.current.get(pageNumber) !== pendingResource
          ) {
            return;
          }
          try {
            const readyResource: CachedReaderPage = {
              chapterId: readerChapterId,
              kind: "ready",
              url: URL.createObjectURL(blob),
              blob,
              decoded: false,
              decodedBytes: 0,
            };
            pageResourcesRef.current.set(pageNumber, readyResource);
            releaseEvictedReaderPages(
              pageCacheRef.current.set(pageNumber, readyResource, getPinnedPageNumbers()),
              pageResourcesRef.current,
              suppressedPreloadPagesRef.current,
            );
          } catch (error: unknown) {
            pageResourcesRef.current.set(pageNumber, {
              chapterId: readerChapterId,
              kind: "error",
              error,
            });
          }
          setPageResources(new Map(pageResourcesRef.current));
        })
        .catch((error: unknown) => {
          if (
            pendingResource.controller.signal.aborted ||
            pageResourcesRef.current.get(pageNumber) !== pendingResource
          ) {
            return;
          }
          pageResourcesRef.current.set(pageNumber, {
            chapterId: readerChapterId,
            kind: "error",
            error,
          });
          setPageResources(new Map(pageResourcesRef.current));
        });
    }

    if (changed) setPageResources(new Map(pageResourcesRef.current));
  }, [
    chapter,
    mode,
    page,
    readerChapterId,
    readerSettings.cache_limit_mb,
    effectivePreloadAhead,
    visiblePageRange?.chapterId,
    visiblePageRange?.firstPage,
    visiblePageRange?.lastPage,
    retryCount,
  ]);

  useEffect(() => {
    if (mode !== "vertical" || pendingScrollPageRef.current !== page) return;
    const viewport = verticalViewportRef.current;
    if (viewport !== null) {
      viewport.scrollTop = (page - 1) * verticalPageStep;
      refreshVisiblePageRange(viewport.scrollTop, viewport.clientHeight);
    }
    pendingScrollPageRef.current = null;
  }, [mode, page, refreshVisiblePageRange, verticalPageStep]);

  useEffect(() => () => {
    clearReaderPageResources(pageCacheRef.current, pageResourcesRef.current);
  }, []);

  if (chapterId.length === 0) {
    return (
      <section className={styles.page} aria-labelledby="reader-title">
        <h1 id="reader-title">Capítulo no especificado</h1>
        <Link className={styles.textLink} to={readerReturnHref}>{readerReturnLabel}</Link>
      </section>
    );
  }

  if (chapterQuery.isPending) {
    return (
      <section className={styles.page} aria-labelledby="reader-title">
        <Link className={styles.textLink} to={readerReturnHref}>{readerReturnLabel}</Link>
        <h1 id="reader-title">Lector</h1>
        <p className={styles.statusMessage} role="status">Cargando información del capítulo…</p>
      </section>
    );
  }

  if (chapterQuery.isError) {
    const details = errorDetails(chapterQuery.error);
    const notFound = chapterQuery.error instanceof ApiError && chapterQuery.error.status === 404;
    return (
      <section className={styles.page} aria-labelledby="reader-title">
        <Link className={styles.textLink} to={readerReturnHref}>{readerReturnLabel}</Link>
        <div className={styles.errorState} role="alert">
          <div>
            <h1 id="reader-title">{notFound ? "Capítulo no disponible" : "No se pudo abrir el capítulo"}</h1>
            <p>{notFound ? "El capítulo no está disponible para lectura." : details.message}</p>
            <p className={styles.errorDetails}>Código: <code>{details.code}</code></p>
            {details.traceId !== null ? (
              <p className={styles.errorDetails}>Traza: <code>{details.traceId}</code></p>
            ) : null}
          </div>
          <button className={styles.secondaryButton} onClick={() => void chapterQuery.refetch()} type="button">
            Reintentar
          </button>
        </div>
      </section>
    );
  }

  if (chapter === undefined) return null;

  const previousDisabled = page <= 1;
  const nextDisabled = mode === "double"
    ? page + 1 >= chapter.pageCount
    : page >= chapter.pageCount;
  const firstVerticalPage = Math.max(1, page - VERTICAL_WINDOW_BEHIND);
  const lastVerticalPage = verticalWindowLastPage(
    page,
    chapter.pageCount,
    effectivePreloadAhead,
    readerChapterId,
    visiblePageRange,
  );
  const verticalPages: number[] = [];
  if (mode === "vertical") {
    for (let pageNumber = firstVerticalPage; pageNumber <= lastVerticalPage; pageNumber += 1) {
      verticalPages.push(pageNumber);
    }
  }
  const doublePageNumbers = mode === "double"
    ? [page, page + 1].filter((pageNumber) => pageNumber <= chapter.pageCount)
    : [];
  const displayedDoublePageNumbers = rtl
    ? [...doublePageNumbers].reverse()
    : doublePageNumbers;
  const pagePositionLabel = mode === "double" && page + 1 <= chapter.pageCount
    ? `Páginas ${page}–${page + 1} de ${chapter.pageCount}`
    : `Página ${page} de ${chapter.pageCount}`;

  const goToPage = (requestedPage: number) => {
    const nextPage = Math.max(1, Math.min(chapter.pageCount, requestedPage));
    pendingScrollPageRef.current = nextPage;
    updatePagePosition(nextPage, true);
    if (mode === "vertical" && verticalViewportRef.current !== null) {
      verticalViewportRef.current.scrollTop = (nextPage - 1) * verticalPageStep;
      refreshVisiblePageRange(
        verticalViewportRef.current.scrollTop,
        verticalViewportRef.current.clientHeight,
      );
      pendingScrollPageRef.current = null;
    }
  };

  const handleVerticalScroll = (scrollTop: number, viewportHeight: number) => {
    refreshVisiblePageRange(scrollTop, viewportHeight);
    const pageAtViewportTop = Math.floor(scrollTop / verticalPageStep) + 1;
    updatePagePosition(Math.max(1, Math.min(chapter.pageCount, pageAtViewportTop)), true);
  };

  const changeZoom = (nextZoom: number) => {
    pendingScrollPageRef.current = page;
    setFit(null);
    setZoom(Math.max(0.5, Math.min(3, Math.round(nextZoom * 100) / 100)));
  };

  const chooseFit = (nextFit: Exclude<ReaderFit, null>) => {
    pendingScrollPageRef.current = page;
    setZoom(1);
    setFit(nextFit);
  };

  const toggleFullscreen = () => {
    const readerRoot = readerRootRef.current;
    if (readerRoot === null) return;
    setFullscreenMessage("");
    if (document.fullscreenElement === readerRoot) {
      if (typeof document.exitFullscreen !== "function") {
        setFullscreenMessage("El navegador no permite salir de pantalla completa desde Yomira.");
        return;
      }
      void document.exitFullscreen().catch(() => {
        setFullscreenMessage("No se pudo salir de pantalla completa. Usa Escape o los controles del navegador.");
      });
      return;
    }
    if (typeof readerRoot.requestFullscreen !== "function") {
      setFullscreenMessage("Este navegador no admite pantalla completa para el lector.");
      return;
    }
    void readerRoot.requestFullscreen().catch(() => {
      setFullscreenMessage("No se pudo activar pantalla completa. Reintenta desde el lector.");
    });
  };

  const changeMode = (nextMode: ReaderMode) => {
    if (mode === nextMode) return;
    if (nextMode === "vertical") pendingScrollPageRef.current = page;
    updateReaderSettings({ mode: nextMode });
  };

  const changeRtl = (nextRtl: boolean) => {
    updateReaderSettings({ rtl: nextRtl });
  };

  const changeEReaderProfile = (enabled: boolean) => {
    updateReaderSettings({ e_reader: enabled });
  };

  const handleReaderKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (
      event.defaultPrevented ||
      event.ctrlKey ||
      event.altKey ||
      event.metaKey ||
      (event.target instanceof HTMLElement && (
        event.target.isContentEditable ||
        ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)
      ))
    ) return;

    const spreadStep = mode === "double" ? 2 : 1;
    let nextPage: number | null = null;
    let handled = true;
    switch (event.key) {
      case "ArrowRight":
      case "ArrowLeft":
        if (mode === "vertical") {
          handled = false;
          break;
        }
        nextPage = page + ((event.key === "ArrowRight") !== rtl ? spreadStep : -spreadStep);
        break;
      case "PageDown":
      case "ArrowDown":
        nextPage = page + 1;
        break;
      case "PageUp":
      case "ArrowUp":
        nextPage = page - 1;
        break;
      case "f":
      case "F":
        toggleFullscreen();
        break;
      case "+":
      case "=":
        changeZoom(zoom + 0.25);
        break;
      case "-":
        changeZoom(zoom - 0.25);
        break;
      case "0":
        changeZoom(1);
        break;
      default:
        handled = false;
    }

    if (!handled) return;
    event.preventDefault();
    if (nextPage !== null) {
      const canMoveForward = mode === "double"
        ? page + 1 < chapter.pageCount
        : page < chapter.pageCount;
      const canMoveBackward = page > 1;
      if (
        (nextPage > page && canMoveForward) ||
        (nextPage < page && canMoveBackward)
      ) goToPage(nextPage);
    }
  };

  const handleTouchPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== "touch" || mode === "vertical") return;
    const target = event.target as HTMLElement;
    if (target.closest("button,a,input,textarea,select,[contenteditable='true']") !== null) return;
    touchStartRef.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      startedAt: event.timeStamp,
    };
  };

  const handleTouchPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = touchStartRef.current;
    touchStartRef.current = null;
    if (
      start === null ||
      event.pointerType !== "touch" ||
      event.pointerId !== start.pointerId ||
      mode === "vertical" ||
      event.timeStamp - start.startedAt > 350 ||
      Math.hypot(event.clientX - start.x, event.clientY - start.y) >= 12
    ) return;

    const target = event.target as HTMLElement;
    if (target.closest("button,a,input,textarea,select,[contenteditable='true']") !== null) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    if (bounds.width <= 0) return;
    const relativeX = (event.clientX - bounds.left) / bounds.width;
    const isForward = relativeX >= 0.7
      ? !rtl
      : relativeX <= 0.3
        ? rtl
        : null;
    if (isForward === null) return;

    const spreadStep = mode === "double" ? 2 : 1;
    const canMoveForward = mode === "double"
      ? page + 1 < chapter.pageCount
      : page < chapter.pageCount;
    if (isForward && canMoveForward) goToPage(page + spreadStep);
    else if (!isForward && page > 1) goToPage(page - spreadStep);
  };

  const markPageDecoded = (
    pageNumber: number,
    naturalWidth: number,
    naturalHeight: number,
  ) => {
    const resource = pageResourcesRef.current.get(pageNumber);
    if (
      resource?.kind !== "ready" ||
      resource.chapterId !== readerChapterId ||
      resource.decoded
    ) return;
    const decodedBytes = Math.max(0, naturalWidth * naturalHeight * 4);
    const decodedResource: CachedReaderPage = {
      ...resource,
      decoded: true,
      decodedBytes,
    };
    pageResourcesRef.current.set(pageNumber, decodedResource);
    releaseEvictedReaderPages(
      pageCacheRef.current.set(pageNumber, decodedResource, getPinnedPageNumbers()),
      pageResourcesRef.current,
      suppressedPreloadPagesRef.current,
    );
    setPageResources(new Map(pageResourcesRef.current));
  };

  const markPageDecodeError = (pageNumber: number) => {
    const resource = pageResourcesRef.current.get(pageNumber);
    if (resource?.kind !== "ready" || resource.chapterId !== readerChapterId) return;
    const cachedResource = pageCacheRef.current.delete(pageNumber)?.resource;
    URL.revokeObjectURL(cachedResource?.url ?? resource.url);
    pageResourcesRef.current.set(pageNumber, {
      chapterId: readerChapterId,
      kind: "error",
      error: new Error("image_decode_failed"),
    });
    setPageResources(new Map(pageResourcesRef.current));
  };

  const renderPage = (pageNumber: number, vertical: boolean) => {
    const storedResource = pageResources.get(pageNumber);
    const resource = storedResource?.chapterId === readerChapterId ? storedResource : undefined;
    if (resource === undefined || resource.kind === "loading") {
      return <p className={styles.statusMessage} role="status">Cargando página {pageNumber}…</p>;
    }
    if (resource.kind === "error") {
      const details = errorDetails(resource.error);
      const notFound = resource.error instanceof ApiError && resource.error.status === 404;
      return (
        <div className={styles.pageError} role="alert">
          <h2>{notFound ? "Página no encontrada" : "No se pudo cargar la página"}</h2>
          <p>{notFound
            ? `No se encontró la página ${pageNumber} de este capítulo.`
            : details.message}</p>
          <p className={styles.errorDetails}>Código: <code>{details.code}</code></p>
          {details.traceId !== null ? (
            <p className={styles.errorDetails}>Traza: <code>{details.traceId}</code></p>
          ) : null}
          <button
            className={styles.secondaryButton}
            onClick={() => {
              pageResourcesRef.current.delete(pageNumber);
              setPageResources(new Map(pageResourcesRef.current));
              setRetryCount((current) => current + 1);
            }}
            type="button"
          >
            {vertical ? `Reintentar página ${pageNumber}` : "Reintentar página"}
          </button>
        </div>
      );
    }
    return (
      <img
        alt={`${chapter.displayLabel}, página ${pageNumber} de ${chapter.pageCount}`}
        className={pageImageClass(vertical, fit)}
        onLoad={(event) => markPageDecoded(
          pageNumber,
          event.currentTarget.naturalWidth,
          event.currentTarget.naturalHeight,
        )}
        onError={() => markPageDecodeError(pageNumber)}
        src={resource.url}
        style={{
          transform: fit === null ? `scale(${zoom})` : undefined,
          transformOrigin: "center",
          "--reader-page-height": `${verticalPageHeight}px`,
        } as CSSProperties}
      />
    );
  };

  return (
    <section
      className={`${styles.page} ${eReader ? styles.ereaderProfile : ""}`}
      aria-labelledby="reader-title"
      onKeyDown={handleReaderKeyDown}
      ref={readerRootRef}
    >
      <header className={styles.pageHeader}>
        <div>
          <Link className={styles.textLink} to={`/series/${chapter.seriesId}`}>
            ← Volver a la serie
          </Link>
          <p className={styles.eyebrow}>Lector</p>
          <h1 id="reader-title">{chapter.displayLabel}</h1>
        </div>
      </header>

      {progressQuery.isError ? (() => {
        const details = errorDetails(progressQuery.error);
        return (
          <div className={styles.progressFailure} role="alert">
            <p>No se pudo cargar el progreso de lectura: {details.message}</p>
            <p className={styles.errorDetails}>Código: <code>{details.code}</code></p>
            {details.traceId !== null ? (
              <p className={styles.errorDetails}>Traza: <code>{details.traceId}</code></p>
            ) : null}
            <button
              className={styles.secondaryButton}
              onClick={() => void progressQuery.refetch()}
              type="button"
            >
              Reintentar progreso
            </button>
          </div>
        );
      })() : null}

      {progressSaveView.chapterId === readerChapterId && progressSaveView.status !== "idle" ? (
        progressSaveView.status === "error" ? (() => {
          const details = errorDetails(progressSaveView.error);
          return (
            <div className={styles.progressFailure} role="alert">
              <p>No se pudo guardar el progreso: {details.message}</p>
              <p className={styles.errorDetails}>Código: <code>{details.code}</code></p>
              {details.traceId !== null ? (
                <p className={styles.errorDetails}>Traza: <code>{details.traceId}</code></p>
              ) : null}
              <button
                className={styles.secondaryButton}
                onClick={() => enqueueChapterProgressSave(readerChapterId, progressSaveView.pageIndex)}
                type="button"
              >
                Reintentar guardado
              </button>
            </div>
          );
        })() : (
          <p className={styles.progressMessage} role="status" aria-live="polite">
            {progressSaveView.status === "saving" ? "Guardando progreso…" : "Progreso guardado"}
          </p>
        )
      ) : null}

      {readerSettingsQuery.isError ? (() => {
        const details = errorDetails(readerSettingsQuery.error);
        return (
          <div className={styles.settingsMessage} role="status">
            <p>No se pudieron cargar las preferencias del lector: {details.message}</p>
            <p className={styles.errorDetails}>Código: <code>{details.code}</code></p>
            {details.traceId !== null ? (
              <p className={styles.errorDetails}>Traza: <code>{details.traceId}</code></p>
            ) : null}
            <button
              className={styles.secondaryButton}
              onClick={() => void readerSettingsQuery.refetch()}
              type="button"
            >
              Reintentar preferencias
            </button>
          </div>
        );
      })() : null}

      {settingsSaveStatus === "error" ? (() => {
        const details = errorDetails(settingsSaveError);
        return (
          <div className={styles.settingsMessage} role="status">
            <p>No se guardaron las preferencias del lector: {details.message}</p>
            <p className={styles.errorDetails}>Código: <code>{details.code}</code></p>
            {details.traceId !== null ? (
              <p className={styles.errorDetails}>Traza: <code>{details.traceId}</code></p>
            ) : null}
            <button
              className={styles.secondaryButton}
              onClick={() => updateReaderSettings(readerSettingsDraft ?? readerSettings)}
              type="button"
            >
              Reintentar preferencias
            </button>
          </div>
        );
      })() : settingsSaveStatus === "saving" ? (
        <p className={styles.settingsMessage} role="status">Guardando preferencias del lector…</p>
      ) : settingsSaveStatus === "saved" ? (
        <p className={styles.settingsMessage} role="status">Preferencias del lector guardadas</p>
      ) : null}

      {fullscreenMessage.length > 0 ? (
        <p className={styles.settingsMessage} role="status">{fullscreenMessage}</p>
      ) : null}

      <div className={styles.modeControls} role="group" aria-label="Modo de lectura">
        <button
          aria-pressed={mode === "single"}
          className={styles.secondaryButton}
          disabled={!settingsAvailable}
          onClick={() => changeMode("single")}
          type="button"
        >
          Página única
        </button>
        <button
          aria-pressed={mode === "vertical"}
          className={styles.secondaryButton}
          disabled={!settingsAvailable}
          onClick={() => changeMode("vertical")}
          type="button"
        >
          Vertical
        </button>
        <button
          aria-pressed={mode === "double"}
          className={styles.secondaryButton}
          disabled={!settingsAvailable}
          onClick={() => changeMode("double")}
          type="button"
        >
          Doble página
        </button>
      </div>

      <div className={styles.readerTools} role="group" aria-label="Controles de vista">
        <button
          aria-label="Derecha a izquierda"
          aria-pressed={rtl}
          className={styles.secondaryButton}
          disabled={!settingsAvailable}
          onClick={() => changeRtl(!rtl)}
          type="button"
        >
          {rtl ? "RTL activado" : "RTL desactivado"}
        </button>
        <button
          aria-label="Alejar"
          className={styles.secondaryButton}
          disabled={zoom <= 0.5}
          onClick={() => changeZoom(zoom - 0.25)}
          type="button"
        >
          −
        </button>
        <span className={styles.zoomValue} aria-live="polite">{Math.round(zoom * 100)} %</span>
        <button
          aria-label="Acercar"
          className={styles.secondaryButton}
          disabled={zoom >= 3}
          onClick={() => changeZoom(zoom + 0.25)}
          type="button"
        >
          +
        </button>
        <button
          aria-pressed={fit === "width"}
          className={styles.secondaryButton}
          onClick={() => chooseFit("width")}
          type="button"
        >
          Ajustar al ancho
        </button>
        <button
          aria-pressed={fit === "height"}
          className={styles.secondaryButton}
          onClick={() => chooseFit("height")}
          type="button"
        >
          Ajustar al alto
        </button>
        <button
          className={styles.secondaryButton}
          onClick={() => changeZoom(1)}
          type="button"
        >
          Restablecer zoom
        </button>
        <button
          aria-pressed={isFullscreen}
          className={styles.secondaryButton}
          onClick={toggleFullscreen}
          type="button"
        >
          {isFullscreen ? "Salir de pantalla completa" : "Pantalla completa"}
        </button>
        <button
          aria-pressed={eReader}
          className={styles.secondaryButton}
          disabled={!settingsAvailable}
          onClick={() => changeEReaderProfile(!eReader)}
          type="button"
        >
          Perfil e-reader
        </button>
      </div>

      <nav aria-label="Navegación de páginas" className={styles.controls}>
        <button
          className={styles.secondaryButton}
          disabled={previousDisabled}
          onClick={() => goToPage(page - (mode === "double" ? 2 : 1))}
          type="button"
        >
          Página anterior
        </button>
        <span aria-live="polite" className={styles.pageNumber}>
          {pagePositionLabel}
        </span>
        <button
          className={styles.secondaryButton}
          disabled={nextDisabled}
          onClick={() => goToPage(page + (mode === "double" ? 2 : 1))}
          type="button"
        >
          Página siguiente
        </button>
      </nav>

      {mode === "single" ? (
        <div
          className={styles.readerCanvas}
          aria-label="Superficie de lectura"
          aria-busy={
            pageResources.get(page)?.chapterId !== readerChapterId ||
            pageResources.get(page)?.kind === "loading"
          }
          onPointerCancel={() => { touchStartRef.current = null; }}
          onPointerDown={handleTouchPointerDown}
          onPointerUp={handleTouchPointerUp}
          role="region"
          tabIndex={0}
        >
          {renderPage(page, false)}
        </div>
      ) : mode === "double" ? (
        <div
          className={`${styles.readerCanvas} ${styles.doubleCanvas}`}
          aria-label="Superficie de lectura"
          aria-busy={displayedDoublePageNumbers.some((pageNumber) => (
            pageResources.get(pageNumber)?.chapterId !== readerChapterId ||
            pageResources.get(pageNumber)?.kind === "loading"
          ))}
          onPointerCancel={() => { touchStartRef.current = null; }}
          onPointerDown={handleTouchPointerDown}
          onPointerUp={handleTouchPointerUp}
          role="region"
          tabIndex={0}
        >
          <div className={styles.doublePages}>
            {displayedDoublePageNumbers.map((pageNumber) => (
              <div className={styles.doublePageSlot} key={pageNumber}>
                {renderPage(pageNumber, false)}
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div
          ref={verticalViewportRef}
          aria-label="Páginas en lectura vertical"
          className={styles.verticalViewport}
          onScroll={(event) => handleVerticalScroll(
            event.currentTarget.scrollTop,
            event.currentTarget.clientHeight,
          )}
          role="region"
          style={{
            "--reader-vertical-page-height": `${verticalPageStep}px`,
            "--reader-page-height": `${verticalPageHeight}px`,
          } as CSSProperties}
          tabIndex={0}
        >
          <div className={styles.verticalTrack}>
            <div
              aria-hidden="true"
              className={styles.verticalSpacer}
              style={{ height: `${(firstVerticalPage - 1) * verticalPageStep}px` }}
            />
            {verticalPages.map((pageNumber) => (
              <article
                aria-busy={
                  pageResources.get(pageNumber)?.chapterId !== readerChapterId ||
                  pageResources.get(pageNumber)?.kind === "loading"
                }
                aria-label={`Página ${pageNumber} de ${chapter.pageCount}`}
                className={styles.verticalPageSlot}
                data-testid="reader-page-slot"
                key={pageNumber}
                role="group"
              >
                {renderPage(pageNumber, true)}
              </article>
            ))}
            <div
              aria-hidden="true"
              className={styles.verticalSpacer}
              style={{ height: `${(chapter.pageCount - lastVerticalPage) * verticalPageStep}px` }}
            />
          </div>
        </div>
      )}
    </section>
  );
}
