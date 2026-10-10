import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import {
  ApiError,
  getRecentLibrary,
  getRecentReaderProgress,
  getSystemReadiness,
  getSeriesCoverUrl,
  type RecentReaderProgress,
} from "../api/client";
import styles from "./HomePage.module.css";
import { SeriesCover } from "../library/SeriesCover";
import { publicationStatus } from "../library/publicationStatus";

const RECENT_LIMIT = 4;

function ProgressCover({ progress }: { progress: RecentReaderProgress }) {
  const [failedSource, setFailedSource] = useState<string | null>(null);
  const source = getSeriesCoverUrl(progress.seriesId, "medium", progress.updatedAt);
  return source === failedSource ? (
    <div className={styles.coverPlaceholder} role="img" aria-label={`Portada no disponible: ${progress.seriesDisplayTitle}`}>
      <img src="/assets/brand/yomira-ice-mark.svg" alt="" width="48" height="48" />
      <span>{progress.seriesDisplayTitle}</span>
    </div>
  ) : (
    <img className={styles.progressCover} src={source} alt={`Portada de ${progress.seriesDisplayTitle}`}
      loading="lazy" decoding="async" onError={() => setFailedSource(source)} />
  );
}

function errorDetails(error: unknown) {
  if (error instanceof ApiError) {
    return {
      message: error.message,
      code: error.code ?? "Sin código de servidor",
      status: error.status,
      traceId: error.traceId,
      retryable: error.retryable,
    };
  }

  return {
    message: "Falló una operación de la interfaz.",
    code: "Sin código de servidor",
    status: null,
    traceId: null,
    retryable: false,
  };
}

function RecentError({
  error,
  label,
  onRetry,
  retryLabel,
  retryDisabled,
  titleId,
}: {
  error: unknown;
  label: string;
  onRetry: () => void;
  retryLabel: string;
  retryDisabled: boolean;
  titleId: string;
}) {
  const details = errorDetails(error);

  return (
    <div aria-labelledby={titleId} className={styles.sectionError} role="alert">
      <div>
        <h3 id={titleId}>{label}</h3>
        <p>{details.message}</p>
        <p className={styles.errorDetails}>
          Código: <code>{details.code}</code>
          {details.status !== null ? <span> · HTTP {details.status}</span> : null}
        </p>
        {details.traceId ? (
          <p className={styles.errorDetails}>
            Traza: <code>{details.traceId}</code>
          </p>
        ) : null}
      </div>
      {details.retryable ? (
        <button
          className={styles.secondaryAction}
          disabled={retryDisabled}
          onClick={onRetry}
          type="button"
        >
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}

function HomePageContent() {
  const readiness = useQuery({
    queryKey: ["system", "readiness"],
    queryFn: getSystemReadiness,
  });
  const serviceReady = readiness.data?.status === "ready";
  const recentLibrary = useQuery({
    queryKey: ["library", "recent", RECENT_LIMIT],
    queryFn: () => getRecentLibrary(RECENT_LIMIT),
    enabled: serviceReady,
  });
  const recentProgress = useQuery({
    queryKey: ["reader", "progress", "recent", RECENT_LIMIT],
    queryFn: () => getRecentReaderProgress(RECENT_LIMIT),
    enabled: serviceReady,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
  });

  const readinessError = readiness.isError ? errorDetails(readiness.error) : null;
  const resume = serviceReady
    ? recentProgress.data?.data.find((progress) => Number(progress.percentage) < 100)
    : undefined;

  return (
    <div className={styles.page}>
      <section aria-labelledby="home-title" className={styles.welcome}>
        <div className={styles.welcomeCopy}>
          <p className={styles.eyebrow}>Tu biblioteca de manga</p>
          <h1 id="home-title">Tu próxima historia <span>empieza aquí.</span></h1>
          <p className={styles.intro}>
            Lee, descubre y continúa tus mangas favoritos. Tu mundo entre páginas, en Yomira.
          </p>
          {resume ? <p className={styles.resumeTitle}>{resume.seriesDisplayTitle}
            <span>{resume.chapterDisplayLabel} · Página {resume.pageIndex + 1}</span></p> : null}
          <div className={styles.heroActions}>
            {resume ? <Link className={styles.primaryAction} to={`/reader/${resume.chapterId}`}
              state={{ originatingSeriesId: resume.seriesId }}>
              <span aria-hidden="true">▶</span> Continuar {resume.chapterDisplayLabel}
            </Link> : <Link className={styles.primaryAction} to="/search">Buscar obras</Link>}
            <Link className={styles.secondaryAction} to="/library">Ver biblioteca</Link>
          </div>
        </div>

        <aside aria-labelledby="service-title" className={serviceReady ? styles.serviceReadyCard : styles.serviceCard}>
          <h2 id="service-title">Servicio local</h2>
          {readiness.isPending ? (
            <p aria-live="polite" className={styles.servicePending} role="status">
              Comprobando preparación del servicio local…
            </p>
          ) : null}
          {serviceReady ? (
            <p className={styles.serviceReady}>Servicio local listo.</p>
          ) : null}
          {readiness.data?.status === "not_ready" ? (
            <div className={styles.serviceUnavailable}>
              <p>El servicio local no está listo.</p>
              <Link className={styles.diagnosticsLink} to="/diagnostics">
                Ver diagnóstico
              </Link>
            </div>
          ) : null}
          {readinessError ? (
            <div aria-labelledby="readiness-error-title" className={styles.serviceError} role="alert">
              <h3 id="readiness-error-title">No se pudo comprobar el servicio local</h3>
              <p>{readinessError.message}</p>
              <p className={styles.errorDetails}>
                Código: <code>{readinessError.code}</code>
                {readinessError.status !== null ? <span> · HTTP {readinessError.status}</span> : null}
              </p>
              {readinessError.traceId ? (
                <p className={styles.errorDetails}>
                  Traza: <code>{readinessError.traceId}</code>
                </p>
              ) : null}
              {readinessError.retryable ? (
                <button
                  className={styles.secondaryAction}
                  disabled={readiness.isFetching}
                  onClick={() => void readiness.refetch()}
                  type="button"
                >
                  Comprobar de nuevo
                </button>
              ) : null}
            </div>
          ) : null}
          {readiness.data?.status === "not_ready" ? (
            <button
              className={styles.secondaryAction}
              disabled={readiness.isFetching}
              onClick={() => void readiness.refetch()}
              type="button"
            >
              Comprobar de nuevo
            </button>
          ) : null}
        </aside>
      </section>

      <div className={styles.recentSections}>
        <section aria-labelledby="recent-progress-title" className={styles.recentSection}>
          <header className={styles.sectionHeader}>
            <div>
              <h2 id="recent-progress-title">Continuar leyendo</h2>
              <p className={styles.sectionDescription}>
                Retoma tus capítulos desde donde los dejaste.
              </p>
            </div>
          </header>

          {!serviceReady ? (
            <p aria-live="polite" className={styles.statusMessage} role="status">
              {readiness.isPending
                ? "Comprobando el servicio local…"
                : "El progreso reciente estará disponible cuando el servicio local esté listo."}
            </p>
          ) : null}
          {serviceReady && recentProgress.isPending ? (
            <p aria-live="polite" className={styles.statusMessage} role="status">
              Cargando progreso guardado…
            </p>
          ) : null}
          {serviceReady && recentProgress.isError ? (
            <RecentError
              error={recentProgress.error}
              label="Error en progreso reciente"
              onRetry={() => void recentProgress.refetch()}
              retryLabel="Reintentar progreso reciente"
              retryDisabled={recentProgress.isFetching}
              titleId="recent-progress-error-title"
            />
          ) : null}
          {serviceReady && recentProgress.isSuccess && recentProgress.data.data.length === 0 ? (
            <div className={styles.emptyState}>
              <span aria-hidden="true" className={styles.emptyMark} />
              <p>El progreso guardado de tus capítulos aparecerá aquí.</p>
            </div>
          ) : null}
          {serviceReady && recentProgress.isSuccess && recentProgress.data.data.length > 0 ? (
            <ul className={styles.recentList}>
              {recentProgress.data.data.slice(0, RECENT_LIMIT).map((progress) => (
                <li className={styles.recentItem} key={progress.chapterId}>
                  <article className={styles.progressItem}>
                    <Link
                      aria-label={`Leer ${progress.chapterDisplayLabel} de ${progress.seriesDisplayTitle}`}
                      className={styles.progressChapterLink}
                      to={`/reader/${progress.chapterId}`}
                      state={{ originatingSeriesId: progress.seriesId }}
                    >
                      <ProgressCover progress={progress} />
                      <span className={styles.chapterCaption}><span aria-hidden="true">▶</span> {progress.chapterDisplayLabel}</span>
                    </Link>
                    <Link
                      aria-label={`Ver serie ${progress.seriesDisplayTitle}`}
                      className={styles.progressSeriesLink}
                      to={`/series/${progress.seriesId}`}
                    >
                      {progress.seriesDisplayTitle}
                    </Link>
                    <p className={styles.progressPosition}>
                      Página {progress.pageIndex + 1} · {progress.percentage} %
                    </p>
                    <progress className={styles.readingBar} max={100} value={Number(progress.percentage)}
                      aria-label={`Progreso de ${progress.chapterDisplayLabel} de ${progress.seriesDisplayTitle}`} />
                    <time className={styles.savedAt} dateTime={progress.updatedAt}>
                      Guardado {formatSavedAt(progress.updatedAt)}
                    </time>
                  </article>
                </li>
              ))}
            </ul>
          ) : null}
        </section>

        <section aria-labelledby="recent-library-title" className={styles.recentSection}>
          <header className={styles.sectionHeader}>
            <div>
              <h2 id="recent-library-title">Añadidos recientemente</h2>
              <p className={styles.sectionDescription}>Tus últimas obras añadidas.</p>
            </div>
            <Link className={styles.libraryLink} to="/library">
              Ver todos <span aria-hidden="true"> ›</span>
            </Link>
          </header>

          {!serviceReady ? (
            <p aria-live="polite" className={styles.statusMessage} role="status">
              {readiness.isPending
                ? "Comprobando el servicio local…"
                : "La biblioteca reciente estará disponible cuando el servicio local esté listo."}
            </p>
          ) : null}
          {serviceReady && recentLibrary.isPending ? (
            <p aria-live="polite" className={styles.statusMessage} role="status">
              Cargando obras recientes…
            </p>
          ) : null}
          {serviceReady && recentLibrary.isError ? (
            <RecentError
              error={recentLibrary.error}
              label="Error en biblioteca reciente"
              onRetry={() => void recentLibrary.refetch()}
              retryLabel="Reintentar biblioteca reciente"
              retryDisabled={recentLibrary.isFetching}
              titleId="recent-library-error-title"
            />
          ) : null}
          {serviceReady && recentLibrary.isSuccess && recentLibrary.data.data.length === 0 ? (
            <div className={styles.emptyState}>
              <span aria-hidden="true" className={styles.emptyMark} />
              <p>Todavía no hay obras recientes en tu biblioteca.</p>
              <Link className={styles.secondaryAction} to="/search">
                Buscar obras
              </Link>
            </div>
          ) : null}
          {serviceReady && recentLibrary.isSuccess && recentLibrary.data.data.length > 0 ? (
            <ul className={styles.recentList}>
              {recentLibrary.data.data.slice(0, RECENT_LIMIT).map((series) => (
                <li className={styles.recentItem} key={series.seriesId}>
                  <Link
                    aria-label={series.displayTitle}
                    className={styles.seriesLink}
                    to={`/series/${series.seriesId}`}
                  >
                    <SeriesCover className={styles.recentCover} series={series} variant="medium" />
                    <span className={styles.seriesCopy}>
                      <span className={styles.itemTitle}>{series.displayTitle}</span>
                      {series.author !== null ? (
                        <span className={styles.itemMetadata}>Autor: {series.author || "Sin asignar"}</span>
                      ) : null}
                      {series.genres?.length ? (
                        <span className={styles.itemMetadata}>{series.genres.join(", ")}</span>
                      ) : null}
                      {series.status || series.language ? (
                        <span className={styles.itemMetadata}>
                          {series.status ? `Publicación: ${publicationStatus(series.status)}` : ""}
                          {series.status && series.language ? " · " : ""}
                          {series.language ? `Idioma: ${series.language}` : ""}
                        </span>
                      ) : null}
                    </span>
                    <span aria-hidden="true" className={styles.seriesArrow}>›</span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      </div>
    </div>
  );
}

function formatSavedAt(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat("es", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

export function HomePage() {
  return <HomePageContent />;
}


