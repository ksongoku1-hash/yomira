import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useEffect, useRef, useState } from "react";

import {
  ApiError,
  applyExternalSeriesMetadata,
  clearMetadataOverride,
  getExternalMetadataCandidates,
  getConnectorCover,
  getReaderChaptersPage,
  getReaderDownloadSource,
  getChapterAvailability,
  getRecentReaderProgress,
  getSeriesDetail,
  getSeriesCoverUrl,
  refreshSeriesMetadata,
  setMetadataOverride,
} from "../api/client";
import type {
  MetadataField,
  MetadataResolution,
  MetadataValue,
  ReaderChapter,
} from "../api/client";
import styles from "./SeriesDetailPage.module.css";
import { SeriesExportAction } from "../usb/SeriesExportAction";
import { SeriesCover } from "../library/SeriesCover";
import { IllustratedBackdrop } from "../library/IllustratedBackdrop";
import { publicationStatus } from "../library/publicationStatus";
import { OnlineChapters } from "./OnlineChapters";
import { ActionIcon } from "../app/ActionIcon";

const READER_CHAPTER_PAGE_SIZE = 100;

function chapterSearchTitle(title: string): string {
  return title.trim().replace(/\s+/g, " ").replace(/\s+\(\d{4}\)$/, "");
}

const metadataLabels: Record<MetadataField, string> = {
  title: "Título",
  cover: "Portada",
  author: "Autor",
  description: "Descripción",
  genres: "Géneros",
  status: "Estado de publicación",
  language: "Idioma",
};

function formatMetadataValue(
  field: MetadataField,
  value: MetadataValue,
  sourceConflict: boolean,
): string {
  if (value === null) return sourceConflict ? "Las fuentes discrepan" : "Sin datos guardados";
  if (field === "cover") {
    return typeof value === "string" && value.startsWith("http")
      ? "Portada disponible en una fuente"
      : "Portada asignada";
  }
  if (Array.isArray(value)) return value.length > 0 ? value.join(", ") : "Lista vacía";
  if (field === "status") return publicationStatus(value);
  return value.length > 0 ? value : "Valor vacío guardado";
}

function authorityLabel(metadata: MetadataResolution): string {
  if (metadata.authority === "USER_OVERRIDE") return "Corrección local";
  if (metadata.authority === "LOCAL_CANONICAL") return "Biblioteca local";
  if (metadata.authority === "SOURCE_METADATA") return "Fuente";
  return metadata.sourceConflict ? "Conflicto entre fuentes" : "Sin origen";
}

function SeriesLoading() {
  return (
    <p aria-live="polite" className={styles.statusMessage} role="status">
      Cargando detalle de la serie.
    </p>
  );
}

function SeriesError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const apiError = error instanceof ApiError ? error : null;
  const notFound = apiError?.status === 404;

  return (
    <div aria-labelledby="series-error-title" className={styles.errorState} role="alert">
      <div>
        <h2 id="series-error-title">
          {notFound ? "No se encontró esta serie" : "No se pudo cargar el detalle"}
        </h2>
        <p>{apiError?.message ?? "Falló una operación de la interfaz."}</p>
        <p className={styles.errorDetails}>
          Código: <code>{apiError?.code ?? "Sin código de servidor"}</code>
          {apiError?.status !== null && apiError?.status !== undefined ? (
            <span> · HTTP {apiError.status}</span>
          ) : null}
        </p>
        {apiError?.traceId ? (
          <p className={styles.errorDetails}>
            Traza: <code>{apiError.traceId}</code>
          </p>
        ) : null}
      </div>
      {!notFound ? (
        <div className={styles.actions}>
          <button className={styles.secondaryButton} onClick={onRetry} type="button">
            Reintentar
          </button>
        </div>
      ) : null}
    </div>
  );
}

function ReaderChapterError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const apiError = error instanceof ApiError ? error : null;

  return (
    <div aria-labelledby="reader-chapters-error-title" className={styles.errorState} role="alert">
      <div>
        <h2 id="reader-chapters-error-title">No se pudieron cargar los capítulos</h2>
        <p>{apiError?.message ?? "No se pudo consultar el contenido local."}</p>
        <p className={styles.errorDetails}>
          Código: <code>{apiError?.code ?? "Sin código de servidor"}</code>
          {apiError?.status !== null && apiError?.status !== undefined ? (
            <span> · HTTP {apiError.status}</span>
          ) : null}
        </p>
        {apiError?.traceId ? (
          <p className={styles.errorDetails}>
            Traza: <code>{apiError.traceId}</code>
          </p>
        ) : null}
      </div>
      <button className={styles.secondaryButton} onClick={onRetry} type="button">
        Reintentar
      </button>
    </div>
  );
}

function ReaderChapterRow({
  chapter,
  seriesId,
  currentChapterId,
  percentage,
}: {
  chapter: ReaderChapter;
  seriesId: string;
  currentChapterId?: string | undefined;
  percentage?: string | undefined;
}) {
  return (
    <li className={styles.chapterItem}>
      <div className={styles.chapterMain}>
        <Link
          aria-label={"Leer " + chapter.displayLabel}
          className={styles.chapterLink}
          to={"/reader/" + chapter.chapterId}
          state={{ originatingSeriesId: seriesId }}
          aria-current={chapter.chapterId === currentChapterId ? "page" : undefined}
        >
          <span className={styles.chapterMark} aria-hidden="true" />
          <span className={styles.chapterLabel}>{chapter.displayLabel}</span>
          <span className={styles.chapterState}>{percentage !== undefined ? Number(percentage) >= 100 ? "Leído" : "En lectura" : "Guardado"}</span>
          <span className={styles.chapterPageCount}>
            {chapter.pageCount} {chapter.pageCount === 1 ? "página" : "páginas"}
          </span>
          <span aria-hidden="true" className={styles.chapterArrow}>›</span>
        </Link>
      </div>
    </li>
  );
}

function ReadyChapters({
  seriesId,
  searchTitle,
  searchConnectorId,
  onDownload,
  downloadOpen,
}: {
  seriesId: string;
  searchTitle: string;
  searchConnectorId: string | null;
  onDownload: (() => void) | null;
  downloadOpen: boolean;
}) {
  const [offset, setOffset] = useState(0);
  const navigate = useNavigate();
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [chapterOrder, setChapterOrder] = useState<"asc" | "desc">("asc");
  const [exportOpen, setExportOpen] = useState(false);
  const [localFilter, setLocalFilter] = useState("");
  const [chapterSearch, setChapterSearch] = useState("");
  const startQuery = useQuery({
    queryKey: ["reader-chapters", seriesId, 0, chapterOrder],
    queryFn: () => getReaderChaptersPage({seriesId, limit: READER_CHAPTER_PAGE_SIZE, offset: 0, order: chapterOrder}),
    staleTime: 30_000,
    retry: false,
  });
  const query = useQuery({
    queryKey: ["reader-chapters", seriesId, offset, chapterOrder, ...(chapterSearch ? [chapterSearch] : [])],
    queryFn: () => getReaderChaptersPage({
      seriesId,
      limit: READER_CHAPTER_PAGE_SIZE,
      offset,
      order: chapterOrder,
      query: chapterSearch,
    }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
    retry: false,
  });
  const page = query.data;
  const recentProgress = useQuery({
    queryKey: ["reader", "progress", "recent", 20],
    queryFn: () => getRecentReaderProgress(20),
    staleTime: 30_000,
    retry: false,
  });
  const resume = recentProgress.data?.data.find((item) => item.seriesId === seriesId);
  const resumeReading = useMutation({
    mutationFn: async (chapterId: string) => {
      const source = await getReaderDownloadSource(chapterId);
      if (source === null) return `/reader/${chapterId}`;
      const availability = await getChapterAvailability(source.connectorId, source.remoteSeriesId);
      return `/reader/${chapterId}${availability.readyRemoteIds.includes(source.chapterRemoteId) ? "" : "?online=1"}`;
    },
    onSuccess: (href) => { if (mounted.current) navigate(href, {state: {originatingSeriesId: seriesId}}); },
    retry: false,
  });
  const firstChapter = startQuery.data?.data[0];
  const totalPages = page === undefined
    ? 0
    : Math.ceil(page.meta.total / READER_CHAPTER_PAGE_SIZE);
  const currentPage = page === undefined ? 0 : Math.floor(offset / READER_CHAPTER_PAGE_SIZE) + 1;
  const searchParams = new URLSearchParams({
    q: chapterSearchTitle(searchTitle),
  });
  if (searchConnectorId !== null) searchParams.set("connector", searchConnectorId);
  const searchHref = `/search?${searchParams.toString()}`;

  return (
    <section
      aria-busy={query.isPending || query.isPlaceholderData}
      id="chapters"
      aria-labelledby="ready-chapters-title"
      className={styles.chapters}
    >
      <header className={styles.chaptersHeader}>
        <div>
          <p className={styles.eyebrow}>Guardados y comprobados</p>
          <h2 id="ready-chapters-title">Capítulos listos para leer</h2>
        </div>
      </header>

      <div className={styles.onlineTools}>
      {resume || firstChapter ? <Link className={styles.primaryButton}
        to={`/reader/${resume?.chapterId ?? firstChapter!.chapterId}`} state={{ originatingSeriesId: seriesId }}
        aria-disabled={resumeReading.isPending || undefined}
        onClick={(event) => {
          if (resumeReading.isPending) { event.preventDefault(); return; }
          if (resume && !page?.data.some((chapter) => chapter.chapterId === resume.chapterId)) {
            event.preventDefault(); resumeReading.mutate(resume.chapterId);
          }
        }}>
        <ActionIcon name="play" />{resumeReading.isPending ? "Preparando lectura…" : resume ? `Continuar ${resume.chapterDisplayLabel} · Página ${resume.pageIndex + 1}` : `${chapterOrder === "asc" ? "Leer primer capítulo" : "Leer capítulo más reciente"} · ${firstChapter!.displayLabel}`}
      </Link> : null}
      {onDownload ? <button type="button" className={styles.secondaryButton} aria-expanded={downloadOpen}
        aria-controls="series-download-selection" onClick={onDownload}><ActionIcon name="download" />Descargar</button>
        : <Link className={styles.secondaryButton} to={searchHref}><ActionIcon name="download" />Descargar capítulos</Link>}
      </div>
      {resume ? <p className={styles.chapterCount}>Progreso del capítulo: {resume.percentage} % · Página {resume.pageIndex + 1}</p> : null}
      {resumeReading.isError ? <p role="alert">No se pudo comprobar la copia del capítulo. Reintenta Continuar; tu progreso se conserva.</p> : null}

      <form aria-label="Buscar capítulos guardados" className={styles.onlineTools} onSubmit={(event) => {
        event.preventDefault();
        setOffset(0);
        setChapterSearch(localFilter.trim());
      }}>
        <label className={styles.onlineControl}>Buscar en capítulos guardados
          <input type="search" maxLength={200} value={localFilter} onChange={(event) => setLocalFilter(event.target.value)} placeholder="Número o título en todo el manga" />
        </label>
        <button type="submit" className={styles.secondaryButton}>Buscar</button>
        {localFilter || chapterSearch ? <button type="button" className={styles.secondaryButton} onClick={() => {
          setLocalFilter(""); setChapterSearch(""); setOffset(0);
        }}>Limpiar búsqueda</button> : null}
      </form>

      {query.isPending ? (
        <p aria-live="polite" className={styles.statusMessage} role="status">
          Cargando capítulos listos para leer…
        </p>
      ) : null}
      {query.isError ? (
        <ReaderChapterError error={query.error} onRetry={() => void query.refetch()} />
      ) : null}

      {page?.meta.total === 0 && !query.isPlaceholderData ? (
        <div className={onDownload ? styles.statusMessage : styles.emptyChapters}>
          <p role="status">{chapterSearch ? `No hay capítulos guardados que coincidan con «${chapterSearch}».` : "Esta serie todavía no tiene capítulos descargados y verificados para leer."}</p>
          {!onDownload && !chapterSearch ?
          <Link
            className={styles.secondaryButton}
            to={searchHref}
          >
            Buscar capítulos
          </Link>
          : null}
        </div>
      ) : null}

      {page !== undefined && page.meta.total > 0 ? (
        <>
          <p className={styles.chapterCount}>
            {page.meta.total} {page.meta.total === 1 ? "capítulo listo" : "capítulos listos"}{chapterSearch ? ` · Resultados de «${chapterSearch}» en todo el manga` : "."}
          </p>
          {query.isFetching ? <p className={styles.statusMessage} role="status">Actualizando capítulos…</p> : null}
          <div className={styles.chapterTools}>
            <details id="series-export" className={styles.cbrSelectionPanel} onToggle={(event) => {
              if (event.currentTarget.open) setExportOpen(true);
            }}>
              <summary>Exportar manga a USB o lector conectado</summary>
              {exportOpen ? <SeriesExportAction seriesId={seriesId} /> : null}
            </details>
            <button type="button" className={`${styles.secondaryButton} ${styles.orderButton}`}
              aria-label="Cambiar orden de capítulos para leer"
              aria-pressed={chapterOrder === "desc"}
              disabled={query.isPlaceholderData}
              title={chapterOrder === "asc" ? "Ascendente: primero los capítulos iniciales" : "Descendente: primero los capítulos finales"}
              onClick={() => {
                setOffset(0);
                setChapterOrder((current) => current === "asc" ? "desc" : "asc");
              }}>
              Orden: {chapterOrder === "asc" ? "primeros capítulos" : "últimos capítulos"}
            </button>
          </div>
          <ul aria-label="Capítulos disponibles para lectura" className={styles.chapterList}>
            {page.data.map((chapter) => (
              <ReaderChapterRow
                chapter={chapter}
                key={chapter.chapterId}
                seriesId={seriesId}
                currentChapterId={resume?.chapterId}
                percentage={recentProgress.data?.data.find((item) => item.chapterId === chapter.chapterId)?.percentage}
              />
            ))}
          </ul>
          {page.data.length === 0 && !query.isPlaceholderData ? <div className={styles.emptyChapters}>
            <p>Ya no hay capítulos en esta página.</p>
            <button type="button" className={styles.secondaryButton} onClick={() => setOffset(0)}>Volver a la primera página</button>
          </div> : null}
          {totalPages > 1 && page.data.length > 0 ? (
            <nav aria-label="Paginación de capítulos" className={styles.chapterPagination}>
              <button
                className={styles.secondaryButton}
                disabled={offset === 0 || query.isPending || query.isPlaceholderData}
                onClick={() => setOffset(Math.max(0, offset - READER_CHAPTER_PAGE_SIZE))}
                type="button"
              >
                Anterior
              </button>
              <span aria-live="polite">Página {currentPage} de {totalPages}</span>
              <button
                className={styles.secondaryButton}
                disabled={offset + page.data.length >= page.meta.total || query.isPending || query.isPlaceholderData}
                onClick={() => setOffset(offset + READER_CHAPTER_PAGE_SIZE)}
                type="button"
              >
                Siguiente
              </button>
            </nav>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function coverFallbackLabel(metadata: MetadataResolution | undefined): string {
  if (metadata?.sourceConflict) return "Hay portadas en conflicto";
  if (metadata?.authority === "SOURCE_METADATA") return "Portada disponible en una fuente";
  return metadata?.value !== null && metadata?.value !== undefined
    ? "Vista previa no disponible"
    : "Sin portada asignada";
}

function SeriesMetadata({ seriesId }: { seriesId: string }) {
  const [downloadOpen, setDownloadOpen] = useState(false);
  const location = useLocation();
  const from = (location.state as { from?: unknown } | null)?.from;
  const backHref = typeof from === "string" && /^\/(?:library|downloads)(?:\?|$)/.test(from)
    ? from : "/library";
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["series", seriesId],
    queryFn: () => getSeriesDetail(seriesId),
    staleTime: 30_000,
    retry: false,
  });
  useEffect(() => {
    if (location.hash === "#chapters" && query.isSuccess) {
      document.getElementById("chapters")?.scrollIntoView?.({ block: "start" });
    }
  }, [location.hash, query.isSuccess, seriesId]);
  const localSummary = useQuery({
    queryKey: ["reader-chapters", seriesId, 0, "asc"],
    queryFn: () => getReaderChaptersPage({seriesId, limit: READER_CHAPTER_PAGE_SIZE, offset: 0, order: "asc"}),
    enabled: query.isSuccess, staleTime: 30_000, retry: false,
  });
  const invalidateMetadata = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: ["series", seriesId] }),
    queryClient.invalidateQueries({ queryKey: ["library"] }),
    queryClient.invalidateQueries({ queryKey: ["reader", "progress", "recent"] }),
  ]);
  const setOverride = useMutation({
    mutationFn: ({ field, value }: { field: MetadataField; value: MetadataValue }) =>
      setMetadataOverride(seriesId, field, value),
    onSuccess: invalidateMetadata,
  });
  const clearOverride = useMutation({
    mutationFn: (field: MetadataField) => clearMetadataOverride(seriesId, field),
    onSuccess: invalidateMetadata,
  });
  const refreshMetadata = useMutation({
    mutationFn: () => refreshSeriesMetadata(seriesId),
    onSuccess: (updatedSeries) => {
      queryClient.setQueryData(["series", seriesId], updatedSeries);
    },
    onSettled: invalidateMetadata,
  });
  const externalCandidates = useMutation({
    mutationFn: () => getExternalMetadataCandidates(seriesId),
  });
  const applyExternalMetadata = useMutation({
    mutationFn: (remoteId: string) => applyExternalSeriesMetadata(seriesId, remoteId),
    onSuccess: (updatedSeries) => {
      queryClient.setQueryData(["series", seriesId], updatedSeries);
      externalCandidates.reset();
    },
    onSettled: invalidateMetadata,
  });
  const metadataBusy = setOverride.isPending || clearOverride.isPending || refreshMetadata.isPending ||
    externalCandidates.isPending || applyExternalMetadata.isPending;
  const resetMetadataErrors = (keepExternalCandidates = false) => {
    setOverride.reset();
    clearOverride.reset();
    refreshMetadata.reset();
    applyExternalMetadata.reset();
    if (!keepExternalCandidates) externalCandidates.reset();
  };
  const series = query.data;
  const [onlineSourceId, setOnlineSourceId] = useState("");
  const onlineSource = series?.readingSources.find((item) => item.seriesSourceId === onlineSourceId)
    ?? series?.readingSources[0];
  const canPreviewCover = onlineSource !== undefined && series?.coverAssetId === null &&
    !series.metadata.find((item) => item.field === "cover")?.hasUserOverride;
  const sourceCover = useQuery({
    queryKey: ["connector-cover", onlineSource?.connectorId, onlineSource?.remoteSeriesId],
    queryFn: () => getConnectorCover(onlineSource!.connectorId, onlineSource!.remoteSeriesId),
    enabled: canPreviewCover,
    staleTime: 60_000, retry: false,
  });
  const incompleteMetadata = series?.metadata.filter((item) =>
    ["author", "description", "genres"].includes(item.field) && item.value === null,
  ).map((item) => metadataLabels[item.field]) ?? [];
  if (series && !series.coverAssetId) incompleteMetadata.unshift("Portada");
  const coverMetadata = series?.metadata.find((item) => item.field === "cover");
  const titleMetadata = series?.metadata.find((item) => item.field === "title");
  const savedSynopsis = series?.metadata.find((item) => item.field === "description");
  const synopsis = savedSynopsis?.value ?? (savedSynopsis?.hasUserOverride ? null : onlineSource?.description);
  const pageTitle =
    titleMetadata === undefined
      ? series?.displayTitle ?? "Detalle de serie"
      : typeof titleMetadata.value === "string" && titleMetadata.value.length > 0
        ? titleMetadata.value
        : titleMetadata.hasUserOverride && titleMetadata.userOverrideValue === ""
          ? "Título vacío guardado"
          : titleMetadata.sourceConflict
            ? "Título en conflicto"
            : "Título sin asignar";
  const searchConnectorIds = new Set(
    (series?.metadata ?? [])
      .flatMap((item) => item.sourceValues.map((source) => source.sourceDisplayName))
      .filter((sourceDisplayName): sourceDisplayName is string =>
        sourceDisplayName?.startsWith("yomira.") === true),
  );
  const searchConnectorId = searchConnectorIds.size === 1
    ? [...searchConnectorIds][0] ?? null
    : null;
  const metadataError = applyExternalMetadata.error ?? externalCandidates.error ??
    refreshMetadata.error ?? setOverride.error ?? clearOverride.error;

  return (
    <section
      aria-busy={query.isPending || metadataBusy}
      aria-labelledby="series-title"
      className={styles.page}
    >
      <IllustratedBackdrop source={series?.coverAssetId
        ? getSeriesCoverUrl(series.seriesId, "large", `${series.coverAssetId}:${series.updatedAt}`)
        : null} />
      <Link className={styles.backLink} aria-label={backHref.startsWith("/downloads") ? "Volver a Descargas" : "Volver a Biblioteca"} to={backHref}><ActionIcon name="back" />{backHref.startsWith("/downloads") ? "Volver a Descargas" : "Volver a Biblioteca"}</Link>
      {!series ? <h1 id="series-title">{pageTitle}</h1> : null}
      {refreshMetadata.isSuccess ? (
        <p aria-live="polite" className={styles.statusMessage} role="status">
          {incompleteMetadata.length === 0 ? "Metadatos y portada actualizados."
            : `Búsqueda terminada. Quedan datos pendientes: ${incompleteMetadata.join(", ")}. Puedes buscar y elegir una ficha externa.`}
        </p>
      ) : null}
      {applyExternalMetadata.isSuccess ? (
        <p aria-live="polite" className={styles.statusMessage} role="status">
          Ficha externa vinculada; se conservaron tus datos existentes.
        </p>
      ) : null}

      {query.isPending ? <SeriesLoading /> : null}
      {query.isError ? (
        <SeriesError error={query.error} onRetry={() => void query.refetch()} />
      ) : null}

      {series ? (
        <article aria-labelledby="series-title" className={styles.detailCard}>
          <SeriesCover
            className={styles.cover}
            fallbackLabel={coverFallbackLabel(coverMetadata)}
            previewSource={canPreviewCover ? sourceCover.data?.data.coverUrl ?? null : null}
            series={series}
            variant="large"
          />
          <div className={styles.detailContent}>
      <header className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>En tu biblioteca</p>
          {series ? <p className={styles.seriesFacts}>{[onlineSource?.author ?? series.author, publicationStatus(onlineSource?.status ?? series.status ?? ""), onlineSource?.language ?? series.language].filter(Boolean).join(" · ")}</p> : null}
          <h1 id="series-title">{pageTitle}</h1>
          {typeof synopsis === "string" && synopsis.length > 0
            ? <p className={styles.summary}>{synopsis}</p> : null}
          {synopsis && savedSynopsis?.value === null && onlineSource
            ? <p className={styles.statusMessage}>Sinopsis de {onlineSource.displayName}</p> : null}
        </div>
        <details className={styles.contextMenu} onKeyDown={(event) => {
          if (event.key === "Escape") { event.currentTarget.open = false; event.currentTarget.querySelector("summary")?.focus(); }
        }}><summary aria-label="Más acciones del manga" title="Más acciones del manga"><ActionIcon name="more" /></summary><div className={styles.menuContent}>
          {series ? (
            <button
              className={styles.secondaryButton}
              disabled={query.isPending || metadataBusy}
              onClick={() => {
                resetMetadataErrors();
                refreshMetadata.mutate();
              }}
              type="button"
            >
              {refreshMetadata.isPending
                ? "Actualizando metadatos y portada…"
                : "Actualizar metadatos y portada"}
            </button>
          ) : null}
          {(localSummary.data?.meta.total ?? 0) > 0 ? <a className={styles.secondaryButton} href="#series-export" onClick={(event) => {
            document.getElementById("series-export")?.setAttribute("open", "");
            event.currentTarget.closest("details")?.removeAttribute("open");
          }}>Exportar y administrar archivos</a> : null}
          <a className={styles.secondaryButton} href="#metadata-title" onClick={() => { document.getElementById("series-metadata")?.setAttribute("open", ""); }}>Información y correcciones</a>
          <Link className={styles.secondaryButton} to="/downloads">Administrar descargas</Link>
        </div></details>
      </header>


            <ReadyChapters
              searchTitle={series.displayTitle || series.canonicalTitle}
              searchConnectorId={searchConnectorId}
              seriesId={seriesId}
              onDownload={onlineSource ? () => setDownloadOpen(!downloadOpen) : null}
              downloadOpen={downloadOpen}
            />
            {onlineSource ? <OnlineChapters key={seriesId} seriesId={seriesId} sources={series.readingSources}
              source={onlineSource} onSourceChange={setOnlineSourceId} downloadOpen={downloadOpen} /> : <section className={styles.chapters} aria-labelledby="online-source-title">
                <p className={styles.eyebrow}>Disponible sin descargar</p>
                <h2 id="online-source-title">Leer online</h2>
                <p className={styles.statusMessage}>Elige una fuente con capítulos para leer esta serie online.</p>
                <Link className={styles.secondaryButton} to={`/search?${new URLSearchParams({q: chapterSearchTitle(series.displayTitle || series.canonicalTitle)})}`}>
                  Elegir fuente para lectura online
                </Link>
              </section>}
            <details id="series-metadata" className={styles.provenance}>
              <summary><h2 id="metadata-title">Metadatos y procedencia</h2></summary>
              <p className={styles.statusMessage}>
                Solo se completan los campos ausentes. Tus correcciones y los datos de la fuente tienen prioridad.
              </p>
              {incompleteMetadata.length > 0 ? <p className={styles.statusMessage} role="status">
                Faltan datos: {incompleteMetadata.join(", ")}. La lectura de los capítulos ya guardados está disponible.
              </p> : null}
              <button
                className={styles.secondaryButton}
                disabled={query.isPending || metadataBusy}
                onClick={() => {
                  resetMetadataErrors();
                  externalCandidates.mutate();
                }}
                type="button"
              >
                {externalCandidates.isPending ? "Buscando datos externos…" : "Buscar datos externos"}
              </button>
              {externalCandidates.data ? (
                <>
                  <p aria-live="polite" className={styles.statusMessage}>
                    {externalCandidates.data.meta.total === 0
                      ? "No se encontraron fichas en MangaDex para esta serie."
                      : externalCandidates.data.meta.total > externalCandidates.data.meta.limit
                        ? `Se muestran hasta ${externalCandidates.data.meta.limit} de ${externalCandidates.data.meta.total} coincidencias en MangaDex. Comprueba el título, el año y el autor antes de elegir.`
                        : "Fichas de MangaDex. Comprueba el título, el año y el autor antes de elegir."}
                    {externalCandidates.data.data.length > 0 ? " Comprueba también la edición: original, a color o recopilación. La ficha elegida conserva tus capítulos y correcciones." : null}
                  </p>
                  {externalCandidates.data.data.length > 0 ? (
                    <ul aria-label="Fichas encontradas en MangaDex" className={styles.sourceValues}>
                      {externalCandidates.data.data.map((candidate) => (
                        <li key={candidate.remoteId}>
                          <span>
                            {[candidate.title, candidate.year, candidate.author]
                              .filter((value) => value !== null && value !== "").join(" · ")}
                          </span>
                          <button
                            className={styles.sourceButton}
                            disabled={query.isPending || metadataBusy}
                            onClick={() => {
                              resetMetadataErrors(true);
                              applyExternalMetadata.mutate(candidate.remoteId);
                            }}
                            type="button"
                          >
                            {applyExternalMetadata.isPending && applyExternalMetadata.variables === candidate.remoteId
                              ? "Actualizando ficha externa…"
                              : "Usar esta ficha"}
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </>
              ) : null}
              <dl className={styles.metadata}>
                <div className={styles.metadataItem}>
                  <dt>Título canónico</dt>
                  <dd>{series.canonicalTitle}</dd>
                </div>
                {series.metadata.map((item) => (
                  <div
                    className={item.field === "description"
                      ? `${styles.metadataItem} ${styles.synopsis}`
                      : styles.metadataItem}
                    key={item.field}
                  >
                    <dt>{metadataLabels[item.field]}</dt>
                    <dd>{formatMetadataValue(item.field, item.value, item.sourceConflict)}</dd>
                    <span className={styles.authorityBadge}>{authorityLabel(item)}</span>
                    {item.sourceConflict ? (
                      <p className={styles.conflictMessage}>
                        {item.hasUserOverride
                          ? "Las fuentes discrepan; se conserva tu corrección."
                          : item.authority !== null
                            ? "Las fuentes discrepan; se conserva el valor de mayor prioridad."
                            : "Elige qué valor guardar para resolver este conflicto."}
                      </p>
                    ) : null}
                    {item.sourceValues.length > 0 ? (
                      <ul
                        aria-label={`Valores recibidos para ${metadataLabels[item.field]}`}
                        className={styles.sourceValues}
                      >
                        {item.sourceValues.map((candidate, index) => {
                          const sourceLabel =
                            candidate.sourceDisplayName ?? `Fuente ${index + 1}`;
                          return (
                            <li key={candidate.seriesSourceId}>
                              <span>
                                {sourceLabel}: {formatMetadataValue(item.field, candidate.value, false)}
                              </span>
                              {item.sourceConflict && item.field !== "cover" ? (
                                <button
                                  aria-label={`Usar el valor de ${sourceLabel} para ${metadataLabels[item.field]}`}
                                  className={styles.sourceButton}
                                  disabled={metadataBusy}
                                  onClick={() => {
                                    resetMetadataErrors();
                                    setOverride.mutate({ field: item.field, value: candidate.value });
                                  }}
                                  type="button"
                                >
                                  Usar este valor
                                </button>
                              ) : null}
                            </li>
                          );
                        })}
                      </ul>
                    ) : null}
                    {item.hasUserOverride ? (
                      <button
                        className={styles.clearOverrideButton}
                        disabled={metadataBusy}
                        onClick={() => {
                          resetMetadataErrors();
                          clearOverride.mutate(item.field);
                        }}
                        type="button"
                      >
                        Quitar corrección
                      </button>
                    ) : null}
                  </div>
                ))}
                <div className={styles.metadataItem}>
                  <dt>Agregada</dt>
                  <dd>
                    <time dateTime={series.createdAt}>{series.createdAt}</time>
                  </dd>
                </div>
                <div className={styles.metadataItem}>
                  <dt>Actualizada</dt>
                  <dd>
                    <time dateTime={series.updatedAt}>{series.updatedAt}</time>
                  </dd>
                </div>
              </dl>
              {metadataError ? (
                <div aria-live="assertive" className={styles.metadataError} role="alert">
                  <p>
                    {metadataError instanceof ApiError
                      ? metadataError.message
                      : "No se pudo guardar el cambio de metadata."}
                  </p>
                  {metadataError instanceof ApiError ? (
                    <p className={styles.errorDetails}>
                      Código: <code>{metadataError.code ?? "Sin código de servidor"}</code>
                      {metadataError.traceId ? (
                        <span> · Traza: <code>{metadataError.traceId}</code></span>
                      ) : null}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </details>
          </div>
        </article>
      ) : null}
    </section>
  );
}

export function SeriesDetailPage() {
  const { seriesId } = useParams();

  return seriesId ? (
    <SeriesMetadata key={seriesId} seriesId={seriesId} />
  ) : (
    <SeriesError
      error={new ApiError({
        code: null,
        message: "No se encontró el identificador de la serie.",
        retryable: false,
        status: null,
        traceId: null,
      })}
      onRetry={() => undefined}
    />
  );
}
