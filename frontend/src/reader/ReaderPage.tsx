import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import {
  ApiError,
  getReaderChapter,
  getReaderDownloadSource,
  getConnectorChapters,
  openOnlineReader,
  getReaderChaptersPage,
  getSeriesDetail,
  getReaderProgress,
  getReaderSettings,
  putReaderProgress,
  putReaderSettings,
} from "../api/client";
import type { ReaderChapter, ReaderSettings } from "../api/client";
import { compareConnectorChapters, currentConnectorChapter } from "../api/chapterOrder";
import { readReaderPreferences, saveReaderPreferences } from "./readerPreferences";
import { anchorAt, fittedPageWidth, readReaderAnchor, saveReaderAnchor, verticalLayout } from "./readerPresentation";
import { prioritizeReaderPage, requestReaderPage } from "./readerPageRequests";
import { prepareOnlineChapter, warmOnlineChapter } from "./prepareOnlineChapter";
import { ReaderPageCache } from "./readerPageCache";
import { ReaderDownloads } from "./ReaderDownloads";
import { ReaderSavedPanel } from "./ReaderSavedPanel";
import { ReaderNextChapter } from "./ReaderNextChapter";
import { readReaderLibrary, type ReaderWorkPreferences } from "./readerLibrary";
import { loadReaderLibrary, putReaderWorkPreferences, readerLibraryError, READER_LIBRARY_QUERY_KEY } from "../api/readerLibrary";
import { detectReaderFormat, formatPreferences } from "./readerFormat";
import { ActionIcon } from "../app/ActionIcon";
import type { CachedReaderPage, EvictedReaderPage } from "./readerPageCache";
import styles from "./ReaderPage.module.css";

type ReaderMode = ReaderSettings["mode"];
type ReaderFit = "width" | "height" | null;
type ReaderSettingsSaveStatus = "idle" | "saving" | "saved" | "error";

class OnlineChapterChangedError extends Error {}

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
  if (error instanceof OnlineChapterChangedError) {
    return { message: error.message, code: "READER_CHAPTER_CHANGED", traceId: null };
  }
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

async function adjacentChapters(seriesId: string, chapterId: string) {
  let previous: ReaderChapter | null = null;
  let found = false;
  for (let offset = 0; ; offset += 100) {
    const result = await getReaderChaptersPage({ seriesId, offset, limit: 100 });
    for (const item of result.data) {
      if (found) return { previous, next: item };
      if (item.chapterId === chapterId) found = true;
      else previous = item;
    }
    if (result.data.length === 0 || offset + result.data.length >= result.meta.total) {
      return { previous: found ? previous : null, next: null };
    }
  }
}

export interface OnlineChapterTarget {
  seriesId: string;
  connectorId: string;
  remoteSeriesId: string;
  remoteId: string;
  displayLabel: string;
}

async function onlineAdjacentChapters(client: QueryClient, seriesId: string, chapterId: string, displayLabel: string) {
  const source = await client.fetchQuery({ queryKey: ["reader-download-source", chapterId],
    queryFn: () => getReaderDownloadSource(chapterId), staleTime: 60_000, retry: false });
  if (source === null) return { previous: null, next: null };
  const catalog = await client.fetchQuery({
    queryKey: ["connector-chapters", source.connectorId, source.remoteSeriesId],
    queryFn: () => getConnectorChapters(source.connectorId, source.remoteSeriesId),
    staleTime: 60_000, retry: false,
  });
  const current = currentConnectorChapter(catalog.data, source.chapterRemoteId, displayLabel);
  if (current === undefined) throw new Error("El capítulo ya no aparece en el catálogo de su fuente.");
  const chapters = catalog.data.filter((item) => item.remoteId !== null && item.language === current.language)
    .sort(compareConnectorChapters);
  const index = chapters.indexOf(current);
  const target = (position: number): OnlineChapterTarget | null => {
    const item = chapters[position];
    if (item == null || item.remoteId === null) return null;
    return { seriesId, connectorId: source.connectorId, remoteSeriesId: source.remoteSeriesId,
      remoteId: item.remoteId, displayLabel: item.displayLabel };
  };
  return { previous: target(index - 1), next: target(index + 1) };
}

export function ReaderPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const online = new URLSearchParams(location.search).get("online") === "1";
  const { chapterId = "" } = useParams();
  const [page, setPage] = useState(1);
  const pendingOnlineProgressRef = useRef<{ chapterId: string; page: number } | null>(null);
  const [readerSettingsDraft, setReaderSettingsDraft] = useState<ReaderSettings | null>(null);
  const [settingsSaveStatus, setSettingsSaveStatus] = useState<ReaderSettingsSaveStatus>("idle");
  const [settingsSaveError, setSettingsSaveError] = useState<unknown>(null);
  const [presentation, setPresentation] = useState(readReaderPreferences);
  const [presentationSaveFailed, setPresentationSaveFailed] = useState(false);
  const [zoom, setZoom] = useState(presentation.zoom);
  const [fit, setFit] = useState<ReaderFit>(presentation.fit);
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
  const [verticalWidth, setVerticalWidth] = useState(768);
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  const [chromeSize, setChromeSize] = useState({ top: 72, bottom: 64 });
  const [pageRatios, setPageRatios] = useState<ReadonlyMap<number, number>>(() => new Map());
  const [immersive, setImmersive] = useState(presentation.immersive);
  const [downloadsOpen, setDownloadsOpen] = useState(false);
  const [chaptersOpen, setChaptersOpen] = useState(false);
  const [controlsVisible, setControlsVisible] = useState(presentation.visibility !== "manual");
  const [scrolling, setScrolling] = useState(false);
  const scrollIdleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const canvasInteractionRef = useRef(false);
  useEffect(() => () => {
    if (scrollIdleTimerRef.current !== null) clearTimeout(scrollIdleTimerRef.current);
  }, []);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [readerLibrary, setReaderLibrary] = useState(readReaderLibrary);
  const [workSaveFailed, setWorkSaveFailed] = useState(false);
  const locallyEditedWorksRef = useRef(new Set<string>());
  const workLayoutRef = useRef<string | null>(null);
  const [savedToolsOpen, setSavedToolsOpen] = useState(false);
  const savedToolsButtonRef = useRef<HTMLButtonElement>(null);
  const automaticallyAppliedRef = useRef<string | null>(null);
  const [navigatorOpen, setNavigatorOpen] = useState(false);
  const [jumpPage, setJumpPage] = useState("1");
  const optionsButtonRef = useRef<HTMLButtonElement>(null);
  const optionsCloseRef = useRef<HTMLButtonElement>(null);
  const navigatorButtonRef = useRef<HTMLButtonElement>(null);
  const jumpInputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (optionsOpen) optionsCloseRef.current?.focus();
    if (navigatorOpen) { jumpInputRef.current?.focus(); jumpInputRef.current?.select(); }
  }, [optionsOpen, navigatorOpen]);
  const [continuous, setContinuous] = useState(presentation.continuous);
  const [autoChapter, setAutoChapter] = useState(presentation.autoChapter);
  const [atVerticalEnd, setAtVerticalEnd] = useState(false);
  const [activity, setActivity] = useState(0);
  const [anchorRevision, setAnchorRevision] = useState(0);
  const [screenWidth, setScreenWidth] = useState(() => window.innerWidth);
  const activityRef = useRef(0);
  const controlsRef = useRef<HTMLDivElement>(null);
  const controlsHidden = !controlsVisible && !chaptersOpen && !savedToolsOpen;
  useEffect(() => {
    setPresentationSaveFailed(!saveReaderPreferences({ ...presentation, zoom, fit, immersive, autoChapter }));
  }, [presentation, zoom, fit, immersive, autoChapter]);
  const anchorRef = useRef({ chapterId, page: 1, fraction: 0 });
  const anchorSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const canvasAnchorRef = useRef<{ chapterId: string; mode: "single" | "double";
    page: number; fraction: number; horizontal: number } | null>(null);
  const canvasSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const canvasRestoredRef = useRef(false);
  const canvasProgramScrollRef = useRef<{ top: number; left: number } | null>(null);
  const flushCanvasAnchor = useCallback(() => {
    if (canvasSaveTimerRef.current !== null) clearTimeout(canvasSaveTimerRef.current);
    const anchor = canvasAnchorRef.current;
    if (anchor !== null) saveReaderAnchor(anchor.chapterId, anchor, anchor.mode);
  }, []);
  const pointersRef = useRef(new Map<number, { x: number; y: number }>());
  const gestureRef = useRef<{ distance: number; zoom: number; x: number; y: number; scrollLeft: number; scrollTop: number } | null>(null);
  const lastTapRef = useRef(0);
  const pageResourcesRef = useRef(new Map<number, ReaderPageResource>());
  const pageCacheRef = useRef(new ReaderPageCache(DEFAULT_READER_SETTINGS.cache_limit_mb * 1_000_000));
  const suppressedPreloadPagesRef = useRef(new Set<number>());
  const cacheChapterIdRef = useRef<string | null>(null);
  const touchStartRef = useRef<ReaderTouchStart | null>(null);
  const verticalViewportRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const programScrollRef = useRef<number | null>(null);
  const scrollInteractionRef = useRef(false);
  const readerRootRef = useRef<HTMLElement | null>(null);
  const pendingScrollPageRef = useRef<number | null>(null);
  const pageRef = useRef(1);
  const routeChapterIdRef = useRef(chapterId);
  const recoveringChapterRef = useRef<ReaderChapter | null>(null);
  const renewImagesRef = useRef(false);
  const automaticRecoveryChapterRef = useRef<string | null>(null);
  const settingsWriteGenerationRef = useRef(0);
  const restoredProgressChapterRef = useRef<string | null>(null);
  const initialProgressSavedChapterRef = useRef<string | null>(null);
  const interactedChapterRef = useRef<string | null>(null);
  const queryClient = useQueryClient();
  const chapterQuery = useQuery({
    queryKey: online ? ["reader-chapter", chapterId, "online"] : ["reader-chapter", chapterId],
    queryFn: async () => {
      if (!online) return getReaderChapter(chapterId);
      const opened = recoveringChapterRef.current?.chapterId === chapterId
        ? await openOnlineReader(chapterId, renewImagesRef.current)
        : await openOnlineReader(chapterId);
      const reopened = opened.chapter;
      const previous = recoveringChapterRef.current;
      if (previous?.chapterId === chapterId &&
        (reopened.seriesId !== previous.seriesId || reopened.pageCount !== previous.pageCount)) {
        throw new OnlineChapterChangedError("La fuente cambió el capítulo. Vuelve a la serie para revisar sus páginas; tu progreso guardado se conserva.");
      }
      if (routeChapterIdRef.current === chapterId && !hasPendingProgressSave(chapterId)) {
        queryClient.setQueryData(["reader-progress", chapterId], opened.progress);
      }
      return reopened;
    },
    ...(online ? { staleTime: 0, refetchOnMount: "always" as const, refetchOnWindowFocus: false, retry: false } : {}),
    enabled: chapterId.length > 0,
  });
  const recoverOnlineEntry = !online && chapterQuery.isError &&
    chapterQuery.error instanceof ApiError && chapterQuery.error.status === 404;
  const entrySource = useQuery({
    queryKey: ["reader-download-source", chapterId],
    queryFn: () => getReaderDownloadSource(chapterId),
    enabled: recoverOnlineEntry,
    retry: false,
    refetchOnWindowFocus: false,
  });
  useEffect(() => {
    if (!recoverOnlineEntry || !entrySource.isSuccess || entrySource.isFetching || entrySource.data === null) return;
    const params = new URLSearchParams(location.search);
    params.set("online", "1");
    navigate(`/reader/${chapterId}?${params.toString()}`, {replace: true, state: location.state});
  }, [recoverOnlineEntry, entrySource.isSuccess, entrySource.isFetching, entrySource.data,
    chapterId, location.search, location.state, navigate]);
  // Only the current source plan can serve online pages; cached metadata is not an open session.
  const chapter = online && (chapterQuery.isFetching || chapterQuery.isError) ? undefined : chapterQuery.data;
  const readerCacheIdentity = chapter === undefined ? null :
    `${chapter.chapterId}:${online ? chapterQuery.dataUpdatedAt : "local"}`;
  const libraryQuery = useQuery({ queryKey: READER_LIBRARY_QUERY_KEY, queryFn: loadReaderLibrary,
    enabled: chapter !== undefined, staleTime: 30_000, retry: false, refetchOnWindowFocus: true });
  const profileSave = useMutation({ mutationFn: ({ seriesId, preferences }: { seriesId: string; preferences: ReaderWorkPreferences }) =>
    putReaderWorkPreferences(seriesId, preferences),
    onMutate: () => queryClient.cancelQueries({ queryKey: READER_LIBRARY_QUERY_KEY }),
    onSuccess: (value) => { queryClient.setQueryData(READER_LIBRARY_QUERY_KEY, value); setWorkSaveFailed(false); },
    onError: () => setWorkSaveFailed(true),
  });
  const { mutate: saveWorkProfile } = profileSave;
  useEffect(() => {
    if (!libraryQuery.data) return;
    const saved = libraryQuery.data;
    setReaderLibrary((current) => ({ ...saved, preferences: [
      ...saved.preferences.filter((item) => !locallyEditedWorksRef.current.has(item.seriesId)),
      ...current.preferences.filter((item) => locallyEditedWorksRef.current.has(item.seriesId)),
    ] }));
  }, [libraryQuery.data]);
  const metadataSeriesId = chapter?.seriesId ?? originatingSeriesIdFromState(location.state);
  const seriesQuery = useQuery({
    queryKey: ["series", metadataSeriesId],
    queryFn: () => getSeriesDetail(metadataSeriesId!),
    enabled: metadataSeriesId !== null,
  });
  const adjacentQuery = useQuery({
    queryKey: ["reader-adjacent", chapter?.seriesId, chapterId, online ? "online" : "local"],
    queryFn: async () => online
      ? onlineAdjacentChapters(queryClient, chapter!.seriesId, chapterId, chapter!.displayLabel)
      : adjacentChapters(chapter!.seriesId, chapterId),
    enabled: chapter !== undefined,
    staleTime: 30_000,
  });
  const onlineTransition = useMutation({
    mutationFn: async (target: OnlineChapterTarget) => {
      return prepareOnlineChapter(queryClient, target);
    },
    retry: false,
  });
  const { mutate: changeOnlineChapter, reset: resetOnlineTransition } = onlineTransition;
  const openAdjacentChapter = useCallback((target: ReaderChapter | OnlineChapterTarget) => {
    if ("chapterId" in target) {
      navigate(`/reader/${target.chapterId}`, { state: { originatingSeriesId: target.seriesId } });
      return;
    }
    changeOnlineChapter(target, { onSuccess: (registered) => {
      if (routeChapterIdRef.current !== chapterId) return;
      navigate(`/reader/${registered.chapterId}?online=1`, { state: { originatingSeriesId: registered.seriesId } });
    } });
  }, [navigate, changeOnlineChapter, chapterId]);
  const returnSeriesId = chapterQuery.data?.seriesId ?? originatingSeriesIdFromState(location.state);
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
  const baseReaderSettings = readerSettingsDraft ?? readerSettingsQuery.data ?? DEFAULT_READER_SETTINGS;
  const workProfile = readerLibrary.preferences.find((item) => item.seriesId === chapter?.seriesId);
  const readerSettings = workProfile ? { ...baseReaderSettings, mode: workProfile.mode, rtl: workProfile.rtl } : baseReaderSettings;
  const detectedFormat = useMemo(() => detectReaderFormat(seriesQuery.data?.genres ?? [],
    routeChapterIdRef.current === chapterId ? [...pageRatios.values()] : []),
    [seriesQuery.data?.genres, pageRatios, chapterId]);
  const applyWorkProfile = useCallback((preferences: ReaderWorkPreferences) => {
    if (!chapter) return;
    flushCanvasAnchor();
    canvasAnchorRef.current = null;
    canvasRestoredRef.current = false;
    pendingScrollPageRef.current = page;
    anchorRef.current = { chapterId: chapter.chapterId, page, fraction: readReaderAnchor(chapter.chapterId, page)?.fraction ?? 0 };
    setContinuous(preferences.continuous);
    locallyEditedWorksRef.current.add(chapter.seriesId);
    saveWorkProfile({ seriesId: chapter.seriesId, preferences });
    setReaderLibrary((current) => ({ ...current, preferences: [
      ...current.preferences.filter((item) => item.seriesId !== chapter.seriesId),
      { ...preferences, seriesId: chapter.seriesId },
    ] }));
  }, [chapter, page, flushCanvasAnchor, saveWorkProfile]);
  useLayoutEffect(() => {
    if (!chapter) return;
    const key = `${chapter.chapterId}:${readerSettings.mode}:${readerSettings.rtl}:${workProfile?.continuous ?? presentation.continuous}`;
    if (workLayoutRef.current !== null && workLayoutRef.current !== key) {
      flushCanvasAnchor(); canvasAnchorRef.current = null; canvasRestoredRef.current = false;
      pendingScrollPageRef.current = pageRef.current;
      anchorRef.current = { chapterId: chapter.chapterId, page: pageRef.current,
        fraction: readReaderAnchor(chapter.chapterId, pageRef.current)?.fraction ?? 0 };
    }
    workLayoutRef.current = key;
    setContinuous(workProfile?.continuous ?? presentation.continuous);
  }, [chapter, readerSettings.mode, readerSettings.rtl, workProfile?.continuous, presentation.continuous, flushCanvasAnchor]);
  useEffect(() => {
    if (!libraryQuery.isSuccess || !chapter || routeChapterIdRef.current !== chapterId || workProfile?.format !== "auto" || !detectedFormat || automaticallyAppliedRef.current === chapter.chapterId) return;
    automaticallyAppliedRef.current = chapter.chapterId;
    // A late metadata response must not change the layout while someone is already reading.
    if (interactedChapterRef.current === chapter.chapterId) return;
    applyWorkProfile(detectedFormat.preferences);
  }, [chapter, chapterId, workProfile?.format, detectedFormat, applyWorkProfile, libraryQuery.isSuccess]);
  const mode = readerSettings.mode === "double" && screenWidth < 700 ? "single" : readerSettings.mode;
  const rtl = readerSettings.rtl;
  const eReader = readerSettings.e_reader;
  const settingsAvailable = readerSettingsQuery.isSuccess;
  const effectivePreloadAhead = eReader ? 0 : readerSettings.preload_ahead;
  const verticalPageStep = verticalPageHeight * Math.max(1, zoom);
  const layout = useMemo(() => verticalLayout(
    chapter?.pageCount ?? 1, verticalWidth, verticalPageHeight,
    pageRatios, zoom, continuous ? 0 : 12, fit === "height",
  ), [chapter?.pageCount, verticalWidth, verticalPageHeight, pageRatios, zoom, continuous, fit]);
  const refreshVisiblePageRange = useCallback((scrollTop: number, viewportHeight: number) => {
    const nextRange =
      mode === "vertical" && chapter !== undefined && readerChapterId !== undefined
        ? viewportHeight > 0 ? {
          chapterId: readerChapterId,
          firstPage: anchorAt(layout.offsets, scrollTop).page,
          lastPage: anchorAt(layout.offsets, scrollTop + viewportHeight - 1).page,
        } : null
        : null;
    setVisiblePageRange((currentRange) =>
      sameVisiblePageRange(currentRange, nextRange) ? currentRange : nextRange,
    );
  }, [chapter, mode, readerChapterId, layout]);
  const progressQuery = useQuery({
    queryKey: ["reader-progress", readerChapterId ?? chapterId],
    queryFn: () => readerChapterId === undefined
      ? Promise.resolve(null)
      : getReaderProgress(readerChapterId),
    enabled: readerChapterId !== undefined,
    staleTime: online ? 30_000 : 0,
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
      const firstVisible = anchorAt(layout.offsets, viewport.scrollTop).page;
      const lastVisible = anchorAt(layout.offsets, viewport.scrollTop + viewport.clientHeight - 1).page;
      for (let pageNumber = firstVisible; pageNumber <= lastVisible; pageNumber += 1) {
        pinned.add(pageNumber);
      }
      return pinned;
    }
    return new Set([page]);
  };
  const visibleImagesReady = readerChapterId !== undefined && [...getPinnedPageNumbers()].every((number) => {
    const resource = pageResources.get(number);
    return resource?.chapterId === readerChapterId && resource.kind === "ready";
  });
  const currentPageResource = pageResources.get(page);
  const canBookmark = progressQuery.isSuccess && progressQuery.fetchStatus !== "fetching" &&
    currentPageResource?.chapterId === readerChapterId && currentPageResource?.kind === "ready" && currentPageResource.decoded;
  const jumpTarget = Number(jumpPage);
  const validJump = chapter !== undefined && Number.isSafeInteger(jumpTarget)
    && jumpPage.trim() !== "" && jumpTarget >= 1 && jumpTarget <= chapter.pageCount;
  const previewPages = navigatorOpen && chapter !== undefined && validJump
    ? Array.from({ length: Math.min(3, chapter.pageCount) }, (_, index) =>
      Math.min(Math.max(1, jumpTarget - 1), Math.max(1, chapter.pageCount - 2)) + index)
    : [];

  const updateReaderSettings = (updates: Partial<ReaderSettings>) => {
    if (!settingsAvailable) return;
    const nextSettings: ReaderSettings = { ...baseReaderSettings, ...updates };
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
      const resource = pageResourcesRef.current.get(nextPage);
      if (!online || (resource?.kind === "ready" && resource.decoded)) {
        pendingOnlineProgressRef.current = null;
        enqueueChapterProgressSave(readerChapterId, nextPage - 1);
      } else {
        pendingOnlineProgressRef.current = { chapterId: readerChapterId, page: nextPage };
      }
    }
  };

  useEffect(() => {
    if (routeChapterIdRef.current === chapterId) return;
    routeChapterIdRef.current = chapterId;
    flushCanvasAnchor();
    canvasAnchorRef.current = null;
    canvasRestoredRef.current = false;
    setPage(1);
    pageRef.current = 1;
    pendingScrollPageRef.current = 1;
    restoredProgressChapterRef.current = null;
    initialProgressSavedChapterRef.current = null;
    interactedChapterRef.current = null;
    pendingOnlineProgressRef.current = null;
    recoveringChapterRef.current = null;
    renewImagesRef.current = false;
    automaticRecoveryChapterRef.current = null;
    setDownloadsOpen(false); setChaptersOpen(false);
    resetOnlineTransition();
    anchorRef.current = { chapterId, page: 1, fraction: 0 };
    setPageRatios(new Map());
    setAtVerticalEnd(false);
    setControlsVisible(presentation.visibility !== "manual");
    setScrolling(false);
    canvasInteractionRef.current = false;
    if (scrollIdleTimerRef.current !== null) clearTimeout(scrollIdleTimerRef.current);
    scrollIdleTimerRef.current = null;
    setNavigatorOpen(false); setSavedToolsOpen(false);
    programScrollRef.current = null;
    scrollInteractionRef.current = false;
    setProgressSaveView({ chapterId, status: "idle" });
  }, [chapterId, resetOnlineTransition, presentation.visibility, flushCanvasAnchor]);

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
        void queryClient.invalidateQueries({ queryKey: ["reader", "progress", "recent"] });
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
    const requestedBookmark = location.state && typeof location.state === "object" && "readerBookmarkPage" in location.state
      ? location.state.readerBookmarkPage : null;
    const bookmarkPage = originatingSeriesIdFromState(location.state) === chapter.seriesId &&
      typeof requestedBookmark === "number" && Number.isSafeInteger(requestedBookmark) &&
      requestedBookmark >= 1 && requestedBookmark <= chapter.pageCount ? requestedBookmark : null;
    const restoredPage = bookmarkPage ?? (progressQuery.data === null
      ? 1
      : Math.min(chapter.pageCount, progressQuery.data.pageIndex + 1));
    const savedAnchor = progressQuery.data === null ? null : readReaderAnchor(readerChapterId, restoredPage);
    anchorRef.current = { chapterId: readerChapterId, page: restoredPage, fraction: savedAnchor?.fraction ?? 0 };
    pendingScrollPageRef.current = restoredPage;
    setAnchorRevision((current) => current + 1);
    const bookmarkAtCurrentPage = bookmarkPage !== null && pageRef.current === restoredPage;
    updatePagePosition(restoredPage, bookmarkPage !== null);
    if (bookmarkPage !== null) {
      if (bookmarkAtCurrentPage) {
        interactedChapterRef.current = readerChapterId;
        if (online) pendingOnlineProgressRef.current = { chapterId: readerChapterId, page: restoredPage };
        else enqueueChapterProgressSave(readerChapterId, restoredPage - 1);
      }
      const nextState = { ...location.state };
      delete nextState.readerBookmarkPage;
      navigate(`${location.pathname}${location.search}`, { replace: true, state: nextState });
    }
  }, [
    chapter,
    progressQuery.data,
    progressQuery.fetchStatus,
    progressQuery.isSuccess,
    readerChapterId,
    location.state, location.pathname, location.search, navigate, online,
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
      setScreenWidth(window.innerWidth);
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

    // Restore the saved page before spending bandwidth on the initial page window.
    if (progressQuery.isPending || page !== pageRef.current) return;

    let changed = false;
    if (cacheChapterIdRef.current !== readerCacheIdentity) {
      changed = clearReaderPageResources(pageCacheRef.current, pageResourcesRef.current);
      suppressedPreloadPagesRef.current.clear();
      cacheChapterIdRef.current = readerCacheIdentity;
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
        ? Math.min(chapter.pageCount, page + 1 + effectivePreloadAhead)
      : Math.min(chapter.pageCount, page + effectivePreloadAhead);
    const desiredPages = new Set<number>();
    for (let pageNumber = firstPage; pageNumber <= lastPage; pageNumber += 1) {
      desiredPages.add(pageNumber);
    }
    for (const pageNumber of previewPages) desiredPages.add(pageNumber);

    const currentOrVisiblePages = new Set([page]);
    if (mode === "double" && page < chapter.pageCount) currentOrVisiblePages.add(page + 1);
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

    // Keep initial bandwidth for visible images before fetching neighbours or previews.
    const requestedPages = [...desiredPages].sort((left, right) => {
      if ((left < page) !== (right < page)) return left < page ? 1 : -1;
      return left >= page ? left - right : right - left;
    });
    for (const pageNumber of requestedPages) {
      if (suppressedPreloadPagesRef.current.has(pageNumber)) continue;
      const activeResource = pageResourcesRef.current.get(pageNumber);
      if (activeResource !== undefined) {
        if (activeResource.kind === "loading" && currentOrVisiblePages.has(pageNumber)) {
          prioritizeReaderPage(activeResource.controller.signal);
        }
        if (activeResource.kind === "ready") pageCacheRef.current.get(pageNumber);
        continue;
      }
      const cachedResource = pageCacheRef.current.get(pageNumber);
      if (cachedResource !== undefined && cachedResource.chapterId === readerChapterId) {
        pageResourcesRef.current.set(pageNumber, cachedResource);
        changed = true;
        continue;
      }
      if (!visibleImagesReady && !currentOrVisiblePages.has(pageNumber)) continue;
      const pendingResource: ReaderPageResource = {
        chapterId: readerChapterId,
        kind: "loading",
        controller: new AbortController(),
      };
      pageResourcesRef.current.set(pageNumber, pendingResource);
      changed = true;

      void requestReaderPage(
        readerChapterId,
        pageNumber,
        pendingResource.controller.signal,
        currentOrVisiblePages.has(pageNumber) ? "visible" : "preload",
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
    readerCacheIdentity,
    effectivePreloadAhead,
    visiblePageRange?.chapterId,
    visiblePageRange?.firstPage,
    visiblePageRange?.lastPage,
    retryCount,
    navigatorOpen,
    jumpPage,
    progressQuery.isPending,
    visibleImagesReady,
  ]);

  useEffect(() => {
    if (!online || chapter === undefined || automaticRecoveryChapterRef.current === chapterId) return;
    const missingSession = (error: unknown) => error instanceof ApiError && error.status === 404;
    const firstVisible = mode === "vertical" && visiblePageRange?.chapterId === chapterId
      ? Math.min(page, visiblePageRange.firstPage) : page;
    const lastVisible = mode === "vertical" && visiblePageRange?.chapterId === chapterId
      ? Math.max(page, visiblePageRange.lastPage) : mode === "double" ? page + 1 : page;
    const missingPage = [...pageResources].some(([number, resource]) =>
      number >= firstVisible && number <= lastVisible && resource.chapterId === chapterId &&
      resource.kind === "error" && missingSession(resource.error));
    const missingProgress = progressSaveView.chapterId === chapterId &&
      progressSaveView.status === "error" && missingSession(progressSaveView.error);
    const missingProgressRead = progressQuery.isError && missingSession(progressQuery.error);
    if (!missingPage && !missingProgress && !missingProgressRead) return;
    // One automatic recovery per visit prevents a bad source or another tab from causing a loop.
    automaticRecoveryChapterRef.current = chapterId;
    recoveringChapterRef.current = chapter;
    renewImagesRef.current = false;
    pendingScrollPageRef.current = pageRef.current;
    if (missingProgress) {
      pendingOnlineProgressRef.current = { chapterId, page: pageRef.current };
    }
    setAtVerticalEnd(false);
    void chapterQuery.refetch();
  }, [online, chapter, chapterId, mode, page, pageResources, progressSaveView,
    visiblePageRange, progressQuery.isError, progressQuery.error, chapterQuery.refetch]);

  useLayoutEffect(() => {
    if (mode !== "vertical") return;
    const viewport = verticalViewportRef.current;
    if (viewport !== null) {
      const anchor = anchorRef.current;
      const targetPage = Math.min(chapter?.pageCount ?? 1, pendingScrollPageRef.current ?? anchor.page);
      const fraction = targetPage === anchor.page ? anchor.fraction : 0;
      viewport.scrollTop = layout.offsets[targetPage - 1]! + layout.heights[targetPage - 1]! * fraction;
      programScrollRef.current = viewport.scrollTop;
      refreshVisiblePageRange(viewport.scrollTop, viewport.clientHeight);
    }
    pendingScrollPageRef.current = null;
  }, [mode, layout, refreshVisiblePageRange, chapter?.pageCount, anchorRevision]);

  useLayoutEffect(() => {
    canvasRestoredRef.current = false;
    const viewport = canvasRef.current;
    if (mode === "vertical" || viewport === null || readerChapterId === undefined || chapter === undefined
      || restoredProgressChapterRef.current !== readerChapterId || page !== pageRef.current) return;
    let anchor = canvasAnchorRef.current;
    if (anchor?.chapterId !== readerChapterId || anchor.mode !== mode || anchor.page !== page) {
      flushCanvasAnchor();
      const saved = progressQuery.data == null ? null : readReaderAnchor(readerChapterId, page, mode);
      anchor = { chapterId: readerChapterId, mode, page, fraction: saved?.fraction ?? 0,
        horizontal: saved?.horizontal ?? 0 };
      canvasAnchorRef.current = anchor;
    }
    const pages = mode === "double" && page < chapter.pageCount ? [page, page + 1] : [page];
    if (pages.some((number) => {
      const resource = pageResources.get(number);
      return resource?.kind !== "ready" || !resource.decoded || resource.chapterId !== readerChapterId;
    })) return;
    const images = [...viewport.querySelectorAll("img")];
    if (images.length !== pages.length || images.some((image) => !image.complete || image.naturalHeight <= 0)) return;
    const content = viewport.firstElementChild?.getBoundingClientRect();
    if (content === undefined || content.width <= 0 || content.height <= 0) return;
    viewport.scrollTop = anchor.fraction * content.height;
    viewport.scrollLeft = anchor.horizontal * content.width;
    canvasProgramScrollRef.current = { top: viewport.scrollTop, left: viewport.scrollLeft };
    canvasRestoredRef.current = true;
  }, [mode, readerChapterId, chapter, page, pageResources, pageRatios, canvasSize, zoom, fit,
    anchorRevision, progressQuery.data, flushCanvasAnchor]);

  useEffect(() => {
    const onVisibility = () => { if (document.visibilityState === "hidden") flushCanvasAnchor(); };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", flushCanvasAnchor);
    return () => {
      flushCanvasAnchor();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", flushCanvasAnchor);
    };
  }, [chapterId, flushCanvasAnchor]);

  useEffect(() => {
    const pending = pendingOnlineProgressRef.current;
    const resource = pageResources.get(page);
    if (online && chapter !== undefined && pending?.chapterId === chapterId && pending.page === page &&
      resource?.kind === "ready" && resource.decoded) {
      pendingOnlineProgressRef.current = null;
      enqueueChapterProgressSave(chapterId, page - 1);
    }
  }, [online, chapter, chapterId, page, pageResources]);

  useEffect(() => {
    const viewport = mode === "vertical" ? verticalViewportRef.current : canvasRef.current;
    if (chapter === undefined || viewport === null) return;
    const resize = () => {
      const top = controlsRef.current?.querySelector("header")?.getBoundingClientRect().height || 72;
      const bottom = controlsRef.current?.querySelector("nav")?.parentElement?.getBoundingClientRect().height || 64;
      setChromeSize((current) => current.top === top && current.bottom === bottom ? current : { top, bottom });
      const css = getComputedStyle(viewport);
      const width = Math.max(0, viewport.clientWidth - (parseFloat(css.paddingLeft) || 0) - (parseFloat(css.paddingRight) || 0));
      const height = Math.max(0, viewport.clientHeight - (parseFloat(css.paddingTop) || 0) - (parseFloat(css.paddingBottom) || 0));
      setCanvasSize((current) => current.width === width && current.height === height ? current : { width, height });
      if (mode === "vertical") {
        setVerticalWidth(Math.max(1, Math.min(1100, width || 768)));
        if (height > 0) setVerticalPageHeight(height);
      }
    };
    resize();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(resize);
    observer?.observe(viewport);
    const header = controlsRef.current?.querySelector("header");
    const footer = controlsRef.current?.querySelector("nav")?.parentElement;
    if (header) observer?.observe(header);
    if (footer) observer?.observe(footer);
    window.addEventListener("resize", resize);
    return () => { observer?.disconnect(); window.removeEventListener("resize", resize); };
  }, [mode, chapter, chromeSize, immersive]);

  useEffect(() => {
    const flush = () => {
      const anchor = anchorRef.current;
      if (anchor.chapterId === chapterId && mode === "vertical") saveReaderAnchor(chapterId, anchor);
      if (anchorSaveTimerRef.current !== null) clearTimeout(anchorSaveTimerRef.current);
    };
    const onVisibility = () => { if (document.visibilityState === "hidden") flush(); };
    document.addEventListener("visibilitychange", onVisibility);
    return () => { flush(); document.removeEventListener("visibilitychange", onVisibility); };
  }, [chapterId, mode]);

  useEffect(() => {
    if (presentation.visibility !== "auto" || !controlsVisible || downloadsOpen || chaptersOpen || optionsOpen || navigatorOpen || savedToolsOpen || progressQuery.isError || readerSettingsQuery.isError || fullscreenMessage.length > 0 || progressSaveView.status === "error" || settingsSaveStatus === "error") return;
    const timer = setTimeout(() => {
      if (!controlsRef.current?.contains(document.activeElement)) setControlsVisible(false);
    }, 3000);
    return () => clearTimeout(timer);
  }, [presentation.visibility, controlsVisible, downloadsOpen, chaptersOpen, optionsOpen, navigatorOpen, savedToolsOpen, activity, progressQuery.isError, readerSettingsQuery.isError, fullscreenMessage, progressSaveView.status, settingsSaveStatus]);

  useEffect(() => {
    if (settingsSaveStatus !== "saved") return;
    const timer = setTimeout(() => setSettingsSaveStatus("idle"), 2000);
    return () => clearTimeout(timer);
  }, [settingsSaveStatus]);

  useEffect(() => {
    const next = adjacentQuery.data?.next;
    const current = pageResources.get(page);
    if (!online || effectivePreloadAhead === 0 || chapter === undefined || next == null || !("connectorId" in next) ||
      page < Math.max(2, chapter.pageCount - 1) || current?.kind !== "ready" || !current.decoded) return;
    const timer = setTimeout(() => { void warmOnlineChapter(queryClient, next).catch(() => {}); }, 300);
    return () => clearTimeout(timer);
  }, [online, chapter, page, pageResources, adjacentQuery.data, queryClient, effectivePreloadAhead]);

  useEffect(() => () => {
    clearReaderPageResources(pageCacheRef.current, pageResourcesRef.current);
  }, []);

  if (chapterId.length === 0) {
    return (
      <section className={styles.page} aria-labelledby="reader-title">
        <h1 id="reader-title">Capítulo no especificado</h1>
        <Link className={styles.textLink} to={readerReturnHref} aria-label={readerReturnLabel.replace("← ", "")}>{readerReturnLabel}</Link>
      </section>
    );
  }

  if (chapterQuery.isPending || (online && chapterQuery.isFetching) ||
    (recoverOnlineEntry && (entrySource.isPending || entrySource.isFetching ||
      (entrySource.isSuccess && entrySource.data !== null)))) {
    return (
      <section className={styles.page} aria-labelledby="reader-title">
        <Link className={styles.textLink} to={readerReturnHref} aria-label={readerReturnLabel.replace("← ", "")}>{readerReturnLabel}</Link>
        <h1 id="reader-title">Lector</h1>
        <p className={styles.statusMessage} role="status">{online && recoveringChapterRef.current?.chapterId === chapterId && !renewImagesRef.current
          ? "Restableciendo la lectura en tu página actual…" : "Cargando información del capítulo…"}</p>
      </section>
    );
  }

  if (chapterQuery.isError) {
    const details = errorDetails(chapterQuery.error);
    const notFound = chapterQuery.error instanceof ApiError && chapterQuery.error.status === 404;
    return (
      <section className={styles.page} aria-labelledby="reader-title">
        <Link className={styles.textLink} to={readerReturnHref} aria-label={readerReturnLabel.replace("← ", "")}>{readerReturnLabel}</Link>
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
    if (!Number.isFinite(requestedPage)) return;
    const nextPage = Math.max(1, Math.min(chapter.pageCount, Math.trunc(requestedPage)));
    if (nextPage !== page) {
      flushCanvasAnchor();
      canvasAnchorRef.current = null;
      canvasRestoredRef.current = false;
    }
    anchorRef.current = { chapterId, page: nextPage, fraction: 0 };
    setAtVerticalEnd(false);
    pendingScrollPageRef.current = nextPage;
    updatePagePosition(nextPage, true);
    if (mode !== "vertical" && nextPage !== page && canvasRef.current !== null) {
      canvasRef.current.scrollTop = 0;
      canvasRef.current.scrollLeft = 0;
    }
    if (mode === "vertical" && verticalViewportRef.current !== null) {
      verticalViewportRef.current.scrollTop = layout.offsets[nextPage - 1]!;
      programScrollRef.current = verticalViewportRef.current.scrollTop;
      refreshVisiblePageRange(
        verticalViewportRef.current.scrollTop,
        verticalViewportRef.current.clientHeight,
      );
      pendingScrollPageRef.current = null;
    }
  };

  const showScrollActivity = (viewport: HTMLDivElement) => {
    setScrolling(true);
    if (scrollIdleTimerRef.current !== null) clearTimeout(scrollIdleTimerRef.current);
    scrollIdleTimerRef.current = setTimeout(() => {
      scrollIdleTimerRef.current = null;
      setScrolling(false);
    }, 900);
    if (downloadsOpen || chaptersOpen || optionsOpen || navigatorOpen || savedToolsOpen
      || progressQuery.isError || readerSettingsQuery.isError || fullscreenMessage.length > 0
      || progressSaveView.status === "error" || settingsSaveStatus === "error") return;
    if (controlsRef.current?.contains(document.activeElement)) viewport.focus({ preventScroll: true });
    setControlsVisible(false);
  };

  const handleCanvasScroll = (viewport: HTMLDivElement) => {
    const anchor = canvasAnchorRef.current;
    if (!canvasRestoredRef.current || anchor === null
      || anchor.chapterId !== readerChapterId || anchor.mode !== mode || anchor.page !== page) return;
    const programmed = canvasProgramScrollRef.current;
    if (programmed !== null && Math.abs(programmed.top - viewport.scrollTop) < 1
      && Math.abs(programmed.left - viewport.scrollLeft) < 1) return;
    canvasProgramScrollRef.current = null;
    if (canvasInteractionRef.current) showScrollActivity(viewport);
    const content = viewport.firstElementChild?.getBoundingClientRect();
    if (content === undefined || content.width <= 0 || content.height <= 0) return;
    const saved = { ...anchor, fraction: Math.min(0.999, Math.max(0, viewport.scrollTop / content.height)),
      horizontal: Math.min(0.999, Math.max(-0.999, viewport.scrollLeft / content.width)) };
    canvasAnchorRef.current = saved;
    if (canvasSaveTimerRef.current !== null) clearTimeout(canvasSaveTimerRef.current);
    canvasSaveTimerRef.current = setTimeout(() => saveReaderAnchor(saved.chapterId, saved, saved.mode), 400);
  };

  const handleVerticalScroll = (scrollTop: number, viewportHeight: number) => {
    refreshVisiblePageRange(scrollTop, viewportHeight);
    if (programScrollRef.current !== null && Math.abs(scrollTop - programScrollRef.current) < 1) {
      programScrollRef.current = null;
      return;
    }
    if (!scrollInteractionRef.current) return;
    if (verticalViewportRef.current !== null) showScrollActivity(verticalViewportRef.current);
    const anchor = anchorAt(layout.offsets, scrollTop);
    anchorRef.current = { chapterId, ...anchor };
    const viewport = verticalViewportRef.current;
    const reachedEnd = viewport !== null && viewport.clientHeight > 0 && viewport.scrollHeight > 0
      && scrollTop + viewportHeight >= viewport.scrollHeight - 8
      && pageResources.get(chapter.pageCount)?.kind === "ready";
    setAtVerticalEnd(reachedEnd);
    updatePagePosition(anchor.page, true);
    if (reachedEnd && readerChapterId !== undefined) enqueueChapterProgressSave(readerChapterId, chapter.pageCount - 1);
    if (anchorSaveTimerRef.current !== null) clearTimeout(anchorSaveTimerRef.current);
    anchorSaveTimerRef.current = setTimeout(() => saveReaderAnchor(chapterId, anchorRef.current), 400);
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

  const changeMode = (nextMode: ReaderMode, nextContinuous = continuous) => {
    if (readerSettings.mode === nextMode && continuous === nextContinuous) return;
    applyWorkProfile({ format: "custom", mode: nextMode, rtl, continuous: nextContinuous });
  };

  const changeRtl = (nextRtl: boolean) => {
    applyWorkProfile({ format: "custom", mode: readerSettings.mode, rtl: nextRtl, continuous });
  };

  const changeEReaderProfile = (enabled: boolean) => {
    updateReaderSettings({ e_reader: enabled });
  };

  const handleReaderKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape" && (optionsOpen || navigatorOpen || savedToolsOpen)) {
      event.preventDefault();
      if (savedToolsOpen) { setSavedToolsOpen(false); savedToolsButtonRef.current?.focus(); }
      else if (navigatorOpen) { setNavigatorOpen(false); setSavedToolsOpen(false); navigatorButtonRef.current?.focus(); }
      else { setOptionsOpen(false); optionsButtonRef.current?.focus(); }
      return;
    }
    if (event.key !== "Escape" && event.target instanceof Element &&
      event.target.closest("#reader-options, #reader-navigator, #reader-saved")) return;
    if (
      event.defaultPrevented ||
      event.ctrlKey ||
      event.altKey ||
      event.metaKey ||
      (event.key !== "Escape" && event.target instanceof HTMLElement && (
        event.target.isContentEditable ||
        ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)
      ))
    ) return;

    if (event.key.toLowerCase() === presentation.shortcut) {
      event.preventDefault();
      if (presentation.visibility === "always") {
        setPresentation((value) => ({ ...value, visibility: "manual" })); setImmersive(true);
      }
      setControlsVisible((visible) => !visible);
      readerRootRef.current?.focus();
      return;
    }
    const spreadStep = mode === "double" ? 2 : 1;
    let nextPage: number | null = null;
    let handled = true;
    switch (event.key) {
      case "Escape":
        setDownloadsOpen(false); setChaptersOpen(false);
        setOptionsOpen(false);
        setNavigatorOpen(false); setSavedToolsOpen(false);
        if (document.fullscreenElement === readerRootRef.current) toggleFullscreen();
        setImmersive(false);
        setPresentation((value) => ({ ...value, visibility: "always" }));
        setControlsVisible(true);
        readerRootRef.current?.focus();
        break;
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
        if (mode === "vertical" || (canvasRef.current !== null &&
          canvasRef.current.scrollHeight > canvasRef.current.clientHeight + 1)) {
          handled = false;
          break;
        }
        nextPage = page + 1;
        break;
      case "PageUp":
      case "ArrowUp":
        if (mode === "vertical" || (canvasRef.current !== null &&
          canvasRef.current.scrollHeight > canvasRef.current.clientHeight + 1)) {
          handled = false;
          break;
        }
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
    if (event.pointerType !== "touch") return;
    const target = event.target as HTMLElement;
    if (target.closest("button,a,input,textarea,select,[contenteditable='true']") !== null) return;
    touchStartRef.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      startedAt: event.timeStamp,
    };
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    canvasInteractionRef.current = true;
    canvasProgramScrollRef.current = null;
    if ((event.target as HTMLElement).closest("button,a,input,summary") !== null) return;
    handleTouchPointerDown(event);
    if (event.pointerType !== "touch" && zoom <= 1) return;
    pointersRef.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const points = [...pointersRef.current.values()];
    const distance = points.length === 2 ? Math.hypot(points[0]!.x - points[1]!.x, points[0]!.y - points[1]!.y) : 0;
    gestureRef.current = { distance, zoom, x: event.clientX, y: event.clientY,
      scrollLeft: event.currentTarget.scrollLeft, scrollTop: event.currentTarget.scrollTop };
    if (points.length > 1) touchStartRef.current = null;
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!pointersRef.current.has(event.pointerId)) return;
    pointersRef.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const gesture = gestureRef.current;
    if (gesture === null) return;
    const points = [...pointersRef.current.values()];
    if (points.length === 2 && gesture.distance > 0) {
      changeZoom(gesture.zoom * Math.hypot(points[0]!.x - points[1]!.x, points[0]!.y - points[1]!.y) / gesture.distance);
    } else if (zoom > 1 && mode !== "vertical") {
      event.currentTarget.scrollLeft = gesture.scrollLeft + gesture.x - event.clientX;
      event.currentTarget.scrollTop = gesture.scrollTop + gesture.y - event.clientY;
    }
  };

  const handleTouchPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const wasPinching = pointersRef.current.size > 1;
    pointersRef.current.delete(event.pointerId);
    gestureRef.current = null;
    const start = touchStartRef.current;
    touchStartRef.current = null;
    if (
      start === null || wasPinching ||
      event.pointerType !== "touch" ||
      event.pointerId !== start.pointerId ||
      event.timeStamp - start.startedAt > 350 ||
      Math.abs(event.clientY - start.y) >= 60
    ) return;

    const target = event.target as HTMLElement;
    if (target.closest("button,a,input,textarea,select,[contenteditable='true']") !== null) return;
    if (mode === "vertical") {
      if (Math.hypot(event.clientX - start.x, event.clientY - start.y) < 12) setControlsVisible((visible) => presentation.visibility === "always" ? true : !visible);
      return;
    }
    const bounds = event.currentTarget.getBoundingClientRect();
    if (bounds.width <= 0) return;
    const deltaX = event.clientX - start.x;
    if (Math.abs(deltaX) >= 50 && Math.abs(deltaX) > Math.abs(event.clientY - start.y) * 1.5) {
      goToPage(page + ((deltaX < 0) !== rtl ? 1 : -1) * (mode === "double" ? 2 : 1));
      return;
    }
    if (Math.hypot(deltaX, event.clientY - start.y) >= 12) return;
    const relativeX = (event.clientX - bounds.left) / bounds.width;
    if (zoom > 1) {
      if (event.timeStamp - lastTapRef.current < 300 && lastTapRef.current > 0) changeZoom(1);
      lastTapRef.current = event.timeStamp;
      return;
    }
    const isForward = relativeX >= 0.7
      ? !rtl
      : relativeX <= 0.3
        ? rtl
        : null;
    if (isForward === null) {
      if (event.timeStamp - lastTapRef.current < 300 && lastTapRef.current > 0) changeZoom(zoom > 1 ? 1 : 2);
      else setControlsVisible((visible) => presentation.visibility === "always" ? true : !visible);
      lastTapRef.current = event.timeStamp;
      return;
    }

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
    if (resource?.kind !== "ready" || resource.chapterId !== readerChapterId) return;
    if (naturalWidth > 0 && naturalHeight > 0 && pageRatios.get(pageNumber) !== naturalWidth / naturalHeight) {
      setPageRatios((current) => new Map(current).set(pageNumber, naturalWidth / naturalHeight));
    }
    if (mode !== "vertical" && (pageNumber === page || (mode === "double" && pageNumber === page + 1))) {
      setAnchorRevision((current) => current + 1);
    }
    if (resource.decoded) return;
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

  const renewOnlineChapter = () => {
    recoveringChapterRef.current = chapter;
    renewImagesRef.current = true;
    pendingScrollPageRef.current = pageRef.current;
    setAtVerticalEnd(false);
    setOptionsOpen(false);
    void chapterQuery.refetch();
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
          <h2>{notFound ? online ? "No se pudo recuperar la página" : "Página no encontrada" : "No se pudo cargar la página"}</h2>
          <p>{notFound
            ? online
              ? `La página ${pageNumber} no está disponible en la sesión actual. Puedes volver a abrir el capítulo; tu progreso se conserva.`
              : `No se encontró la página ${pageNumber} de este capítulo.`
            : details.message}</p>
          <p className={styles.errorDetails}>Código: <code>{details.code}</code></p>
          {details.traceId !== null ? (
            <p className={styles.errorDetails}>Traza: <code>{details.traceId}</code></p>
          ) : null}
          {online ? <p>Al reintentar se reabrirá el capítulo en tu página actual, sin descargarlo.</p> : null}
          <button
            className={styles.secondaryButton}
            onClick={() => {
              if (online) {
                renewOnlineChapter();
                return;
              }
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
        key={`${readerChapterId}:${pageNumber}:${resource.url}`}
        alt={`${chapter.displayLabel}, página ${pageNumber} de ${chapter.pageCount}`}
        className={pageImageClass(vertical, fit)}
        onLoad={(event) => markPageDecoded(
          pageNumber,
          event.currentTarget.naturalWidth,
          event.currentTarget.naturalHeight,
        )}
        onError={() => markPageDecodeError(pageNumber)}
        src={resource.url}
        draggable={false}
        style={{
          width: vertical
            ? fit !== "height" ? `${verticalWidth * zoom}px` : undefined
            : fittedPageWidth(pageRatios.get(pageNumber),
              mode === "double" ? Math.max(0, (canvasSize.width - 2) / 2) : canvasSize.width,
              canvasSize.height, fit, zoom),
          "--reader-page-height": `${verticalPageHeight}px`,
        } as CSSProperties}
      />
    );
  };

  return (
    <section
      className={`${styles.page} ${scrolling ? styles.scrolling : ""} ${eReader ? styles.ereaderProfile : ""} ${immersive ? styles.immersive : ""} ${continuous ? styles.continuous : ""} ${presentation.compact ? styles.compact : ""} ${nextDisabled || atVerticalEnd ? styles.chapterFinished : ""}`}
      aria-labelledby="reader-title"
      style={{ "--reader-top": `${chromeSize.top}px`, "--reader-bottom": `${chromeSize.bottom}px`,
        "--reader-background": eReader ? "#fff" : presentation.background === "sepia" ? "#e8dcc4" : presentation.background === "light" ? "#e9edf2" : "#080c13",
        "--reader-brightness": eReader ? 1 : presentation.brightness / 100,
        "--reader-page-clip": rtl ? "0 0 0 8%" : "0 8% 0 0",
      } as CSSProperties}
      onKeyDown={handleReaderKeyDown}
      onMouseMove={(event) => {
        if (presentation.visibility !== "auto" || event.buttons !== 0 || scrollIdleTimerRef.current !== null) return;
        if (controlsHidden && (event.target as Element).closest("button,a,input,select,textarea")) return;
        const bounds = event.currentTarget.getBoundingClientRect();
        if (mode === "vertical" && event.clientY > bounds.top + chromeSize.top + 12 && event.clientY < bounds.bottom - chromeSize.bottom - 12) return;
        if (Date.now() - activityRef.current < 500) return;
        activityRef.current = Date.now(); setActivity(activityRef.current); setControlsVisible(true);
      }}
      ref={readerRootRef}
      tabIndex={-1}
    >
      <div className={styles.readerChrome} ref={controlsRef}
        inert={controlsHidden ? true : undefined}
        aria-hidden={controlsHidden ? true : undefined}>
      <header className={styles.pageHeader}>
        <div className={styles.readerIdentity}>
          <Link className={`${styles.secondaryButton} ${styles.iconButton}`} to={readerReturnHref}
            aria-label="Volver a la serie" title="Volver a la serie">
            <ActionIcon name="back" />
          </Link>
          <div className={styles.readerTitle}>
          <p className={styles.eyebrow}>{seriesQuery.data?.displayTitle ?? "Lector"}</p>
          <ReaderDownloads key={`chapters:${chapter.chapterId}`} chapterId={chapter.chapterId}
            headingId="reader-title" displayLabel={chapter.displayLabel}
            open={chaptersOpen} onOpenChange={(open) => {
              setChaptersOpen(open); setDownloadsOpen(false); setOptionsOpen(false); setNavigatorOpen(false); setSavedToolsOpen(false);
            }} navigation={{ chapter, onRead: openAdjacentChapter, isPending: onlineTransition.isPending, isError: onlineTransition.isError }} />
          </div>
        </div>
        <div className={styles.headerActions}>
          <button className={`${styles.secondaryButton} ${styles.iconButton}`} type="button"
            ref={savedToolsButtonRef} aria-label="Marcadores e historial" title="Marcadores e historial"
            aria-controls="reader-saved" aria-expanded={savedToolsOpen} onClick={() => {
              setSavedToolsOpen(!savedToolsOpen); setDownloadsOpen(false); setChaptersOpen(false); setOptionsOpen(false); setNavigatorOpen(false);
            }}><ActionIcon name="bookmark" /></button>
          <ReaderDownloads key={chapter.chapterId} chapterId={chapter.chapterId} displayLabel={chapter.displayLabel} online={online} open={downloadsOpen} onOpenChange={(open) => {
            setDownloadsOpen(open); setChaptersOpen(false); setOptionsOpen(false); setNavigatorOpen(false); setSavedToolsOpen(false);
          }} />
        <button
          aria-pressed={isFullscreen}
          aria-label={isFullscreen ? "Salir de pantalla completa" : "Pantalla completa"}
          title={isFullscreen ? "Salir de pantalla completa (F)" : "Pantalla completa (F)"}
          className={`${styles.secondaryButton} ${styles.iconButton}`}
          onClick={toggleFullscreen}
          type="button"
        >
          <ActionIcon name={isFullscreen ? "exitFullscreen" : "fullscreen"} />
        </button>
          <button className={`${styles.secondaryButton} ${styles.toolbarButton}`} aria-expanded={optionsOpen} aria-controls="reader-options"
            ref={optionsButtonRef}
            aria-label="Opciones de lectura" title="Opciones de lectura"
            onClick={() => { setOptionsOpen(!optionsOpen); setDownloadsOpen(false); setChaptersOpen(false); setNavigatorOpen(false); setSavedToolsOpen(false); }} type="button">
            <ActionIcon name="settings" /><span className={styles.toolbarLabel}>Opciones de lectura</span>
          </button>
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
              onClick={() => updateReaderSettings(baseReaderSettings)}
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

      {savedToolsOpen ? <ReaderSavedPanel chapter={chapter} page={page} canBookmark={canBookmark}
        onPage={goToPage} onClose={() => { setSavedToolsOpen(false); savedToolsButtonRef.current?.focus(); }} /> : null}
      {optionsOpen ? <div id="reader-options" className={styles.optionsPanel}>
        <div className={styles.panelHeading}>
          <div><h2>Configuración de lectura</h2><p className={styles.panelHint}>Los cambios se aplican al instante.</p></div>
          <button ref={optionsCloseRef} type="button" className={`${styles.secondaryButton} ${styles.iconButton}`} aria-label="Cerrar configuración" title="Cerrar configuración" onClick={() => { setOptionsOpen(false); optionsButtonRef.current?.focus(); }}><ActionIcon name="close" /></button>
        </div>
        <div className={styles.optionsPreview} role="img" aria-label="Vista previa de lectura"
          data-theme={eReader ? "light" : presentation.background} data-rtl={rtl}
          data-mode={mode === "vertical" && continuous ? "webtoon" : mode}
          style={{ "--preview-zoom": Math.min(zoom, 1.6), "--preview-brightness": eReader ? 1 : presentation.brightness / 100 } as CSSProperties}>
          <div className={styles.previewStage} aria-hidden="true">
            {Array.from({ length: mode === "single" ? 1 : mode === "double" ? 2 : 3 }, (_, index) =>
              <div key={index} className={styles.previewPage}><i /><i /><i /><i /></div>)}
          </div>
          <span className={styles.previewTag} aria-hidden="true">{rtl ? "RTL" : "LTR"} · {Math.round(zoom * 100)}%</span>
        </div>
        <fieldset className={styles.optionGroup}><legend>Perfil de esta obra</legend>
          <label className={styles.controlLabel} htmlFor="reader-work-format">Tipo y modo de lectura</label>
          <select id="reader-work-format" className={styles.formatSelect} value={workProfile?.format ?? "custom"}
            onChange={(event) => {
              const format = event.currentTarget.value;
              if (format === "auto") {
                automaticallyAppliedRef.current = detectedFormat ? chapter.chapterId : null;
                applyWorkProfile(detectedFormat?.preferences ?? { format: "auto", mode: readerSettings.mode, rtl, continuous });
              } else if (format === "manga" || format === "manhwa" || format === "comic") applyWorkProfile(formatPreferences(format));
              else applyWorkProfile({ format: "custom", mode: readerSettings.mode, rtl, continuous });
            }}>
            <option value="custom">Personalizado</option><option value="auto">Automático</option>
            <option value="manga">Manga</option><option value="manhwa">Manhwa / Webtoon</option><option value="comic">Cómic</option>
          </select>
          <p className={styles.panelHint}>{detectedFormat ? `Detectado: ${detectedFormat.label}. ${detectedFormat.reason}`
            : "Sin datos suficientes para reconocer el formato. Se conservan tus ajustes."}</p>
          <p className={styles.panelHint}>Elige Automático para usar la detección. Tus correcciones se guardan por obra en la biblioteca de Yomira.</p>
          {libraryQuery.isError ? <div role="alert"><p>{readerLibraryError(libraryQuery.error)}</p>
            <button className={styles.secondaryButton} type="button" onClick={() => void libraryQuery.refetch()}>Reintentar datos del lector</button></div> : null}
          {libraryQuery.data?.migrationWarning ? <p role="status">{libraryQuery.data.migrationWarning}</p> : null}
          {profileSave.isPending ? <p role="status">Guardando perfil en la biblioteca…</p> : null}
          {workSaveFailed ? <div role="alert"><p>No se pudo guardar el perfil en la biblioteca. Se aplica durante esta sesión; los datos anteriores se conservan.</p>
            {workProfile ? <button className={styles.secondaryButton} type="button" onClick={() => applyWorkProfile(workProfile)}>Reintentar perfil</button> : null}</div> : null}
        </fieldset>
        <fieldset className={styles.optionGroup}><legend>Lectura</legend>
          <div className={styles.modeTiles} role="group" aria-label="Modo de lectura">
            {([
              ["single", "Página única", "page"], ["vertical", "Vertical", "vertical"],
              ["double", "Doble página", "spread"], ["webtoon", "Webtoon continuo", "continuous"],
            ] as const).map(([nextMode, label, icon]) => <button key={nextMode}
              className={`${styles.secondaryButton} ${styles.optionTile}`} type="button" disabled={!settingsAvailable}
              aria-pressed={(mode === "vertical" && continuous ? "webtoon" : mode) === nextMode}
              onClick={() => {
                if (nextMode === "vertical" || nextMode === "webtoon") setContinuous(nextMode === "webtoon");
                changeMode(nextMode === "webtoon" ? "vertical" : nextMode,
                  nextMode === "vertical" || nextMode === "webtoon" ? nextMode === "webtoon" : continuous);
              }}><ActionIcon name={icon} />{label}</button>)}
          </div>
          {readerSettings.mode === "double" && mode === "single" ? <p className={styles.panelHint}>La doble página se recuperará al ampliar la pantalla.</p> : null}
          <label className={styles.switchRow}><span>Continuar al siguiente capítulo</span>
            <input aria-label="Continuar al siguiente capítulo automáticamente" type="checkbox" checked={autoChapter} onChange={(event) => setAutoChapter(event.target.checked)} />
          </label>
        </fieldset>

        <fieldset className={styles.optionGroup}><legend>Dirección y encuadre</legend>
          <div role="group" aria-label="Controles de vista">
            <div className={styles.segmented} role="group" aria-label="Dirección de lectura">
              <button className={styles.secondaryButton} type="button" aria-label="Derecha a izquierda"
                aria-pressed={rtl} disabled={!settingsAvailable} onClick={() => changeRtl(true)}><ActionIcon name="rtl" />RTL</button>
              <button className={styles.secondaryButton} type="button" aria-label="Izquierda a derecha"
                aria-pressed={!rtl} disabled={!settingsAvailable} onClick={() => changeRtl(false)}>LTR<span className={styles.directionLtr}><ActionIcon name="rtl" /></span></button>
            </div>
            <div className={styles.zoomControls} role="group" aria-label="Zoom">
              <button aria-label="Alejar" title="Alejar" className={styles.secondaryButton} type="button"
                disabled={zoom <= 0.5} onClick={() => changeZoom(zoom - 0.25)}><ActionIcon name="zoomOut" /></button>
              <input className={styles.optionRange} aria-label="Zoom de lectura" type="range" min={50} max={300} step={1}
                value={Math.round(zoom * 100)} onChange={(event) => changeZoom(Number(event.target.value) / 100)} />
              <button aria-label="Acercar" title="Acercar" className={styles.secondaryButton} type="button"
                disabled={zoom >= 3} onClick={() => changeZoom(zoom + 0.25)}><ActionIcon name="zoomIn" /></button>
              <span className={styles.zoomValue} aria-live="polite">{Math.round(zoom * 100)} %</span>
            </div>
            <div className={`${styles.segmented} ${styles.fitControls}`}>
              <button aria-label="Ajustar al ancho" title="Ajustar al ancho" aria-pressed={fit === "width"}
                className={styles.secondaryButton} type="button" onClick={() => chooseFit("width")}><ActionIcon name="fitWidth" />Ancho</button>
              <button aria-label="Ajustar al alto" title="Ajustar al alto" aria-pressed={fit === "height"}
                className={styles.secondaryButton} type="button" onClick={() => chooseFit("height")}><ActionIcon name="fitHeight" />Alto</button>
              <button aria-label="Restablecer zoom" title="Restablecer zoom" className={styles.secondaryButton} type="button"
                onClick={() => changeZoom(1)}><ActionIcon name="refresh" />Restablecer</button>
            </div>
          </div>
          <p className={styles.panelHint}>Las tiras muy largas mantienen un ancho legible en página única y doble.</p>
        </fieldset>
        <fieldset className={styles.optionGroup}><legend>Comodidad visual</legend>
          <div className={styles.backgroundChoices} role="group" aria-label="Fondo de lectura">
            {([ ["dark", "Oscuro"], ["sepia", "Sepia"], ["light", "Claro"] ] as const).map(([background, label]) =>
              <button key={background} className={`${styles.secondaryButton} ${styles.optionTile}`} type="button" disabled={eReader}
                aria-pressed={(eReader ? "light" : presentation.background) === background}
                onClick={() => setPresentation((value) => ({ ...value, background }))}>
                <span className={styles.themeSwatch} data-theme={background} aria-hidden="true" />{label}
              </button>)}
          </div>
          <label className={styles.brightnessControl}><ActionIcon name="sun" />
            <input className={styles.optionRange} aria-label="Brillo de páginas" type="range" min={50} max={100} step={5} disabled={eReader}
              value={eReader ? 100 : presentation.brightness} onChange={(event) => setPresentation((value) => ({ ...value, brightness: Number(event.target.value) }))} />
            <span>{eReader ? 100 : presentation.brightness}%</span>
          </label>
          <p className={styles.panelHint}>{eReader ? "El perfil e-reader mantiene el fondo blanco y las páginas sin filtros." : "El fondo y el brillo no modifican tus archivos."}</p>
        </fieldset>
      <details className={styles.advancedOptions}>
      <summary>Más opciones<ActionIcon name="next" /></summary>
      <fieldset className={styles.optionGroup}><legend>Controles</legend>
      <div className={styles.optionTiles}>
        <button className={`${styles.secondaryButton} ${styles.optionTile}`} aria-pressed={immersive}
          aria-label="Modo inmersivo" onClick={(event) => {
            setImmersive(!immersive); setControlsVisible(true);
            setPresentation((value) => ({ ...value, visibility: !immersive ? "auto" : "always" }));
            if (event.detail > 0) readerRootRef.current?.focus();
          }} type="button"><ActionIcon name="focus" /> Modo inmersivo</button>
        <button aria-pressed={eReader} className={`${styles.secondaryButton} ${styles.optionTile}`} disabled={!settingsAvailable}
          onClick={() => changeEReaderProfile(!eReader)} type="button"><ActionIcon name="device" /> Perfil e-reader</button>
      </div>
      <div className={styles.preferenceFields}>
        <div><p className={styles.controlLabel}>Visibilidad de controles</p>
          <div className={styles.segmented} role="group" aria-label="Visibilidad de controles">
            {([ ["manual", "Manual"], ["auto", "Auto-ocultar"], ["always", "Ocultar al desplazar"] ] as const).map(([visibility, label]) =>
              <button key={visibility} className={styles.secondaryButton} type="button" aria-pressed={presentation.visibility === visibility}
                onClick={() => {
                  setPresentation((value) => ({ ...value, visibility })); setControlsVisible(true);
                  setImmersive(visibility !== "always");
                }}>{label}</button>)}
          </div>
        </div>
        <div><p className={styles.controlLabel}>Atajo de controles</p>
          <div className={styles.shortcutKeys} role="group" aria-label="Atajo de controles">
            {(["h", "c"] as const).map((shortcut) => <button key={shortcut} className={styles.secondaryButton} type="button"
              aria-pressed={presentation.shortcut === shortcut} onClick={() => setPresentation((value) => ({ ...value, shortcut }))}>{shortcut.toUpperCase()}</button>)}
          </div>
        </div>
        <label className={styles.switchRow}><span className={styles.iconLabel}><ActionIcon name="progress" />Barra de progreso</span><input type="checkbox" checked={presentation.progressBar} onChange={(event) => setPresentation((value) => ({ ...value, progressBar: event.target.checked }))} /></label>
        <label className={styles.switchRow}><span className={styles.iconLabel}><ActionIcon name="compact" />Controles compactos</span><input type="checkbox" checked={presentation.compact} onChange={(event) => setPresentation((value) => ({ ...value, compact: event.target.checked }))} /></label>
        {presentationSaveFailed ? <p role="status">El navegador no permite guardar estos ajustes visuales. Se conservan durante esta sesión.</p> : null}
      </div>
      <div className={styles.readerTools}>
        <p className={styles.shortcuts}>Atajos: ← → páginas · + − zoom · 0 restablecer · F pantalla completa · {presentation.shortcut.toUpperCase()} controles · Escape cerrar paneles.</p>
      </div>
      </fieldset>
      {online ? <fieldset className={styles.optionGroup}><legend><span className={styles.iconLabel}><ActionIcon name="refresh" /> Recuperación de imágenes</span></legend>
        <button className={styles.secondaryButton} type="button" disabled={chapterQuery.isFetching}
          onClick={renewOnlineChapter}><ActionIcon name="refresh" /> Renovar imágenes del capítulo</button>
        <p className={styles.panelHint}>Vuelve a pedir las imágenes a la fuente. Conserva tu página actual.</p>
      </fieldset> : null}
      </details>
      </div> : null}

      <div className={styles.bottomBar}>
      <nav aria-label="Navegación de páginas" className={styles.controls}>
        <button
          className={`${styles.secondaryButton} ${styles.pageNavigation}`}
          disabled={previousDisabled}
          aria-label="Página anterior" title="Página anterior"
          onClick={() => goToPage(page - (mode === "double" ? 2 : 1))}
          type="button"
        >
          <ActionIcon name="previous" /><span className={styles.toolbarLabel}>Anterior</span>
        </button>
        <button type="button" className={`${styles.secondaryButton} ${styles.pageNumber}`} aria-expanded={navigatorOpen}
          ref={navigatorButtonRef} title="Ir a otra página"
          aria-controls="reader-navigator" onClick={() => {
            setJumpPage(String(page)); setNavigatorOpen(!navigatorOpen); setDownloadsOpen(false); setChaptersOpen(false); setOptionsOpen(false);
          }}>
          {pagePositionLabel}
        </button>
        <button
          className={`${styles.secondaryButton} ${styles.pageNavigation}`}
          disabled={onlineTransition.isPending || (nextDisabled && !(autoChapter && adjacentQuery.data?.next))}
          aria-label="Página siguiente" title="Página siguiente"
          onClick={() => {
            if (nextDisabled && autoChapter && adjacentQuery.data?.next) openAdjacentChapter(adjacentQuery.data.next);
            else goToPage(page + (mode === "double" ? 2 : 1));
          }}
          type="button"
        >
          <span className={styles.toolbarLabel}>Siguiente</span><ActionIcon name="next" />
        </button>
      </nav>
      {presentation.progressBar ? <input className={styles.progressSlider} aria-label="Progreso del capítulo" type="range" min={1} max={chapter.pageCount} step={mode === "double" ? 2 : 1} value={page} onChange={(event) => goToPage(Number(event.target.value))} /> : null}
      </div>
      {navigatorOpen ? <form id="reader-navigator" className={styles.navigator} onSubmit={(event) => {
        event.preventDefault();
        if (!validJump) return;
        goToPage(jumpTarget); setNavigatorOpen(false); setSavedToolsOpen(false); navigatorButtonRef.current?.focus();
      }}>
        <label>Ir a la página <input ref={jumpInputRef} type="number" min={1} max={chapter.pageCount} required
          value={jumpPage} onChange={(event) => setJumpPage(event.target.value)} /></label>
        <button className={styles.secondaryButton} disabled={!validJump} type="submit">Ir</button>
        <button className={styles.secondaryButton} type="button" onClick={() => { setNavigatorOpen(false); setSavedToolsOpen(false); navigatorButtonRef.current?.focus(); }}>Cerrar páginas</button>
        <div className={styles.thumbnails}>{previewPages.map((number) => {
          const resource = pageResources.get(number);
          return <button className={styles.thumbnail} type="button" key={number} aria-label={`Ir a página ${number}`}
            aria-current={number === page ? "page" : undefined}
            onClick={() => { goToPage(number); setNavigatorOpen(false); setSavedToolsOpen(false); navigatorButtonRef.current?.focus(); }}>
            {resource?.kind === "ready" ? <img src={resource.url} alt={`Miniatura de página ${number}`} draggable={false}
              onLoad={(event) => markPageDecoded(number, event.currentTarget.naturalWidth, event.currentTarget.naturalHeight)} /> : <span>Vista previa</span>}
            <span>{number}</span>
          </button>;
        })}</div>
      </form> : null}
      </div>
      {controlsHidden ? <button type="button" className={styles.revealControls}
        onClick={() => { setControlsVisible(true); readerRootRef.current?.focus(); }}>Mostrar controles</button> : null}

      {mode === "single" ? (
        <div
          ref={canvasRef}
          className={styles.readerCanvas}
          aria-label="Superficie de lectura"
          aria-busy={
            pageResources.get(page)?.chapterId !== readerChapterId ||
            pageResources.get(page)?.kind === "loading"
          }
          onPointerCancel={() => { touchStartRef.current = null; pointersRef.current.clear(); gestureRef.current = null; }}
          onScroll={(event) => handleCanvasScroll(event.currentTarget)}
          onWheel={() => { canvasInteractionRef.current = true; canvasProgramScrollRef.current = null; }}
          onKeyDown={() => { canvasInteractionRef.current = true; canvasProgramScrollRef.current = null; }}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handleTouchPointerUp}
          onDoubleClick={() => changeZoom(zoom > 1 ? 1 : 2)}
          style={{ touchAction: zoom > 1 ? "none" : "pan-y" }}
          role="region"
          tabIndex={0}
        >
          {renderPage(page, false)}
        </div>
      ) : mode === "double" ? (
        <div
          ref={canvasRef}
          className={`${styles.readerCanvas} ${styles.doubleCanvas}`}
          aria-label="Superficie de lectura"
          aria-busy={displayedDoublePageNumbers.some((pageNumber) => (
            pageResources.get(pageNumber)?.chapterId !== readerChapterId ||
            pageResources.get(pageNumber)?.kind === "loading"
          ))}
          onPointerCancel={() => { touchStartRef.current = null; pointersRef.current.clear(); gestureRef.current = null; }}
          onScroll={(event) => handleCanvasScroll(event.currentTarget)}
          onWheel={() => { canvasInteractionRef.current = true; canvasProgramScrollRef.current = null; }}
          onKeyDown={() => { canvasInteractionRef.current = true; canvasProgramScrollRef.current = null; }}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handleTouchPointerUp}
          onDoubleClick={() => changeZoom(zoom > 1 ? 1 : 2)}
          style={{ touchAction: zoom > 1 ? "none" : "pan-y" }}
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
          onWheel={() => { scrollInteractionRef.current = true; programScrollRef.current = null; }}
          onPointerDown={(event) => { scrollInteractionRef.current = true; programScrollRef.current = null; handleTouchPointerDown(event); }}
          onKeyDown={() => { scrollInteractionRef.current = true; programScrollRef.current = null; }}
          onPointerUp={handleTouchPointerUp}
          onPointerCancel={() => { touchStartRef.current = null; }}
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
          <div className={styles.verticalTrack} style={{ minWidth: fit !== "height" ? verticalWidth * zoom : undefined }}>
            <div
              aria-hidden="true"
              className={styles.verticalSpacer}
              style={{ height: `${layout.offsets[firstVerticalPage - 1]}px` }}
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
                style={{ height: `${layout.heights[pageNumber - 1]}px` }}
              >
                {renderPage(pageNumber, true)}
              </article>
            ))}
            <div
              aria-hidden="true"
              className={styles.verticalSpacer}
              style={{ height: `${layout.offsets[chapter.pageCount]! - layout.offsets[lastVerticalPage]!}px` }}
            />
          </div>
        </div>
      )}
      {nextDisabled || atVerticalEnd ? <footer className={styles.chapterEnd}>
        <div><strong>Final del capítulo</strong><span>{chapter.displayLabel}</span></div>
        {adjacentQuery.isError ? <button className={styles.secondaryButton} type="button" onClick={() => void adjacentQuery.refetch()}>Reintentar capítulos</button> : null}
        {onlineTransition.isPending ? <p role="status">Abriendo capítulo online…</p> : null}
        {onlineTransition.isError ? <p role="alert">No se pudo abrir el capítulo. Puedes reintentar con sus botones; tu progreso se conserva.</p> : null}
        {adjacentQuery.data?.previous ? ("chapterId" in adjacentQuery.data.previous
          ? <Link className={styles.secondaryButton} to={`/reader/${adjacentQuery.data.previous.chapterId}`} state={{ originatingSeriesId: chapter.seriesId }}>Capítulo anterior</Link>
          : <button className={styles.secondaryButton} type="button" disabled={onlineTransition.isPending} onClick={() => openAdjacentChapter(adjacentQuery.data!.previous!)}>Capítulo anterior</button>) : null}
        {adjacentQuery.data?.next ? <ReaderNextChapter next={adjacentQuery.data.next}
          automatic={autoChapter && atVerticalEnd && mode === "vertical" && progressSaveView.status === "saved" && !onlineTransition.isPending && !onlineTransition.isError}
          disabled={onlineTransition.isPending} onOpen={openAdjacentChapter} onCancel={() => setAutoChapter(false)} />
          : <Link className={styles.secondaryButton} to={readerReturnHref}>Volver a la ficha</Link>}
      </footer> : null}
    </section>
  );
}
