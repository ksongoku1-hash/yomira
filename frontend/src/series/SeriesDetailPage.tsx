import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { useState } from "react";

import {
  ApiError,
  applyExternalSeriesMetadata,
  clearMetadataOverride,
  getExternalMetadataCandidates,
  getReaderChaptersPage,
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

const READER_CHAPTER_PAGE_SIZE = 100;

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
}: {
  chapter: ReaderChapter;
  seriesId: string;
  currentChapterId?: string | undefined;
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
}: {
  seriesId: string;
  searchTitle: string;
  searchConnectorId: string | null;
}) {
  const [offset, setOffset] = useState(0);
  const [chapterOrder, setChapterOrder] = useState<"asc" | "desc">("asc");
  const [exportOpen, setExportOpen] = useState(false);
  const query = useQuery({
    queryKey: ["reader-chapters", seriesId, offset, chapterOrder],
    queryFn: () => getReaderChaptersPage({
      seriesId,
      limit: READER_CHAPTER_PAGE_SIZE,
      offset,
      order: chapterOrder,
    }),
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
  const firstChapter = page?.data[0];
  const totalPages = page === undefined
    ? 0
    : Math.ceil(page.meta.total / READER_CHAPTER_PAGE_SIZE);
  const currentPage = page === undefined ? 0 : Math.floor(offset / READER_CHAPTER_PAGE_SIZE) + 1;
  const searchParams = new URLSearchParams({
    q: searchTitle.trim().replace(/\s+/g, " ").replace(/\s+\(\d{4}\)$/, ""),
  });
  if (searchConnectorId !== null) searchParams.set("connector", searchConnectorId);
  const searchHref = `/search?${searchParams.toString()}`;

  return (
    <section
      aria-busy={query.isPending}
      aria-labelledby="ready-chapters-title"
      className={styles.chapters}
    >
      <header className={styles.chaptersHeader}>
        <div>
          <p className={styles.eyebrow}>Guardados y comprobados</p>
          <h2 id="ready-chapters-title">Capítulos listos para leer</h2>
        </div>
        <button type="button" className={`${styles.secondaryButton} ${styles.orderButton}`}
          aria-label="Cambiar orden de capítulos para leer"
          aria-pressed={chapterOrder === "desc"}
          title={chapterOrder === "asc" ? "Ascendente: primero los capítulos iniciales" : "Descendente: primero los capítulos finales"}
          onClick={() => {
            setOffset(0);
            setChapterOrder((current) => current === "asc" ? "desc" : "asc");
          }}>
          <span aria-hidden="true">{chapterOrder === "asc" ? "↑" : "↓"}</span>
        </button>
      </header>

      {query.isPending ? (
        <p aria-live="polite" className={styles.statusMessage} role="status">
          Cargando capítulos listos para leer…
        </p>
      ) : null}
      {query.isError ? (
        <ReaderChapterError error={query.error} onRetry={() => void query.refetch()} />
      ) : null}

      {page?.meta.total === 0 ? (
        <div className={styles.emptyChapters}>
          <p>Esta serie todavía no tiene capítulos descargados y verificados para leer.</p>
          <Link
            className={styles.secondaryButton}
            to={searchHref}
          >
            Buscar capítulos
          </Link>
        </div>
      ) : null}

      {page !== undefined && page.meta.total > 0 ? (
        <>
          {resume || firstChapter ? <Link className={styles.primaryButton}
            to={`/reader/${resume?.chapterId ?? firstChapter!.chapterId}`} state={{ originatingSeriesId: seriesId }}>
            {resume ? `Continuar ${resume.chapterDisplayLabel} · Página ${resume.pageIndex + 1}` : `${chapterOrder === "asc" ? "Leer primer capítulo" : "Leer capítulo más reciente"} · ${firstChapter!.displayLabel}`}
          </Link> : null}
          <p className={styles.chapterCount}>
            {page.meta.total} {page.meta.total === 1 ? "capítulo listo." : "capítulos listos."}
          </p>
          <Link className={styles.secondaryButton} to={searchHref}>Buscar capítulos faltantes</Link>
          <details className={styles.cbrSelectionPanel} onToggle={(event) => {
            if (event.currentTarget.open) setExportOpen(true);
          }}>
            <summary>Exportar manga a USB o lector conectado</summary>
            {exportOpen ? <SeriesExportAction seriesId={seriesId} /> : null}
          </details>
          <ul aria-label="Capítulos disponibles para lectura" className={styles.chapterList}>
            {page.data.map((chapter) => (
              <ReaderChapterRow
                chapter={chapter}
                key={chapter.chapterId}
                seriesId={seriesId}
                currentChapterId={resume?.chapterId}
              />
            ))}
          </ul>
          {totalPages > 1 ? (
            <nav aria-label="Paginación de capítulos" className={styles.chapterPagination}>
              <button
                className={styles.secondaryButton}
                disabled={offset === 0 || query.isPending}
                onClick={() => setOffset(Math.max(0, offset - READER_CHAPTER_PAGE_SIZE))}
                type="button"
              >
                Anterior
              </button>
              <span aria-live="polite">Página {currentPage} de {totalPages}</span>
              <button
                className={styles.secondaryButton}
                disabled={offset + page.data.length >= page.meta.total || query.isPending}
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
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["series", seriesId],
    queryFn: () => getSeriesDetail(seriesId),
    staleTime: 30_000,
    retry: false,
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
  const incompleteMetadata = series?.metadata.filter((item) =>
    ["author", "description", "genres"].includes(item.field) && item.value === null,
  ).map((item) => metadataLabels[item.field]) ?? [];
  if (series && !series.coverAssetId) incompleteMetadata.unshift("Portada");
  const coverMetadata = series?.metadata.find((item) => item.field === "cover");
  const titleMetadata = series?.metadata.find((item) => item.field === "title");
  const synopsis = series?.metadata.find((item) => item.field === "description")?.value;
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
      <header className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>Detalle local</p>
          <h1 id="series-title">{pageTitle}</h1>
          {typeof synopsis === "string" && synopsis.length > 0
            ? <p className={styles.summary}>{synopsis}</p> : null}
        </div>
        <div className={styles.actions}>
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
          <Link className={styles.secondaryButton} to="/library">
            Volver a Biblioteca
          </Link>
        </div>
      </header>

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
            series={series}
            variant="large"
          />
          <div className={styles.detailContent}>
            <ReadyChapters
              searchTitle={series.displayTitle || series.canonicalTitle}
              searchConnectorId={searchConnectorId}
              seriesId={seriesId}
            />
            <details className={styles.provenance}>
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
