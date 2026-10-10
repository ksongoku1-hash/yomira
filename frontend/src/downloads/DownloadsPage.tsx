import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import {
  ApiError,
  archiveDownloadJob,
  cancelDownloadJob,
  getDownloadJobsPage,
  restoreDownloadJob,
  retryDownloadJob,
  type DownloadJobSummary,
  type DownloadJobStatus,
  type DownloadJobsPage,
} from "../api/client";
import styles from "./DownloadsPage.module.css";

const PAGE_SIZE = 50;
const cancellableStatuses = new Set<DownloadJobStatus>([
  "QUEUED",
  "FETCHING_METADATA",
  "DOWNLOADING",
  "VALIDATING",
  "PROCESSING",
  "PACKAGING",
  "VERIFYING",
  "INDEXING",
]);
const terminalStatuses = new Set<DownloadJobStatus>(["COMPLETED", "FAILED", "CANCELLED"]);

const statusLabels: Record<DownloadJobStatus, string> = {
  QUEUED: "En cola",
  FETCHING_METADATA: "Preparando metadatos",
  DOWNLOADING: "Descargando",
  VALIDATING: "Validando",
  PROCESSING: "Procesando",
  PACKAGING: "Preparando archivo",
  VERIFYING: "Verificando",
  INDEXING: "Añadiendo a biblioteca",
  COMPLETED: "Completado",
  FAILED: "Fallido",
  CANCELLED: "Cancelado",
  INTERRUPTED: "Interrumpido",
};

function DownloadJobsError({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry: () => void;
}) {
  const apiError = error instanceof ApiError ? error : null;

  return (
    <div className={styles.errorState} role="alert">
      <div>
        <h2>No se pudieron cargar los trabajos</h2>
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
      <button className={styles.secondaryButton} onClick={onRetry} type="button">
        Reintentar carga
      </button>
    </div>
  );
}

function JobActionError({ error, title }: { error: unknown; title: string }) {
  const apiError = error instanceof ApiError ? error : null;

  return (
    <div className={styles.cancellationError} role="alert">
      <h4>{title}</h4>
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
  );
}

function DownloadJobCard({
  job,
  archivedView,
  onJobUpdated,
  onJobRetried,
  onQueueChanged,
  progress,
}: {
  job: DownloadJobSummary;
  archivedView: boolean;
  onJobUpdated: (updatedJob: DownloadJobSummary) => void;
  onJobRetried: () => void;
  onQueueChanged: () => void;
  progress: {knownPages: number; validatedPages: number} | undefined;
}) {
  const cancelMutation = useMutation({
    mutationFn: cancelDownloadJob,
    onSuccess: onJobUpdated,
  });
  const retryMutation = useMutation({
    mutationFn: retryDownloadJob,
    retry: false,
    onSuccess: onJobRetried,
  });
  const archiveMutation = useMutation({
    mutationFn: archiveDownloadJob,
    retry: false,
    onSuccess: onQueueChanged,
  });
  const restoreMutation = useMutation({
    mutationFn: restoreDownloadJob,
    retry: false,
    onSuccess: onQueueChanged,
  });
  const canCancel = cancellableStatuses.has(job.status);
  const cancellationPending =
    job.status !== "CANCELLED" && job.cancelRequestedAt !== null;
  const cancellationPendingMessage =
    job.status === "FAILED"
      ? "La cancelación quedó registrada, pero el trabajo falló antes de alcanzar un punto seguro."
      : job.status === "INTERRUPTED"
        ? "La solicitud sigue registrada mientras el trabajo interrumpido permanece pendiente de recuperación."
        : "Cancelación solicitada; se completará en un punto seguro.";

  return (
    <li className={styles.jobItem}>
      <article className={styles.jobCard} data-status={job.status}>
        <div className={styles.jobDetails}>
          <h3>{job.seriesTitle}</h3>
          <p className={styles.chapterLabel}>{job.chapterLabel}</p>
          <details className={styles.reference}>
            <summary>Detalles del trabajo</summary>
            <code>{job.jobId}</code>
          </details>
          {job.status === "COMPLETED" ? (
            <Link
              aria-label={`Leer ${job.chapterLabel} de ${job.seriesTitle}`}
              className={styles.readerLink}
              to={`/reader/${encodeURIComponent(job.chapterId)}`}
            >
              Abrir en el lector
            </Link>
          ) : null}
        </div>
        <div className={styles.jobState}>
          <span className={styles.statusBadge} data-status={job.status}>
            {statusLabels[job.status]}
          </span>
          {progress && cancellableStatuses.has(job.status) ? (
            <div className={styles.pageProgress}>
              <span>{progress.validatedPages} de {progress.knownPages} páginas comprobadas</span>
              <progress aria-label={`Páginas comprobadas de ${job.chapterLabel}`}
                value={progress.validatedPages} max={progress.knownPages} />
              <small>{progress.validatedPages === progress.knownPages
                ? "Imágenes comprobadas; preparando el archivo para leer."
                : "El capítulo estará listo cuando se comprueben todas las páginas y se prepare el archivo."}</small>
            </div>
          ) : job.status === "DOWNLOADING" || job.status === "FETCHING_METADATA" ? (
            <p className={styles.phaseMessage}>
              {job.status === "FETCHING_METADATA" ? "Consultando el capítulo en la fuente."
                : "La fuente está preparando las imágenes. El conteo aparecerá cuando Yomira las reciba."}
            </p>
          ) : null}
          <span className={styles.priority}>Prioridad: {job.priority}</span>
          <time dateTime={job.createdAt}>
            Creado: {new Date(job.createdAt).toLocaleString("es-CO")}
          </time>
          <time dateTime={job.updatedAt}>
            Actualizado: {new Date(job.updatedAt).toLocaleString("es-CO")}
          </time>
          {cancellationPending ? (
            <p aria-label="Cancelación solicitada" className={styles.cancellationPending} role="status">
              {cancellationPendingMessage}
            </p>
          ) : null}
          {cancelMutation.isPending ? (
            <p className={styles.cancellationPending} role="status">
              Enviando solicitud de cancelación…
            </p>
          ) : null}
          {canCancel && !cancellationPending && !cancelMutation.isPending ? (
            <button
              aria-label={`Cancelar ${job.chapterLabel} de ${job.seriesTitle}`}
              className={styles.cancelButton}
              onClick={() => cancelMutation.mutate(job.jobId)}
              type="button"
            >
              Cancelar descarga
            </button>
          ) : null}
          {job.status === "FAILED" || job.status === "INTERRUPTED" ? (
            <button
              aria-label={`Reintentar ${job.chapterLabel} de ${job.seriesTitle}`}
              className={styles.secondaryButton}
              disabled={retryMutation.isPending || retryMutation.isSuccess}
              onClick={() => retryMutation.mutate(job.jobId)}
              type="button"
            >
              {retryMutation.isPending ? "Solicitando reintento…" : "Reintentar descarga"}
            </button>
          ) : null}
          {!archivedView && terminalStatuses.has(job.status) && !cancellationPending ? (
            <button
              aria-label={"Quitar " + job.chapterLabel + " de " + job.seriesTitle + " de la cola"}
              className={styles.secondaryButton}
              disabled={archiveMutation.isPending}
              onClick={() => archiveMutation.mutate(job.jobId)}
              type="button"
            >
              {archiveMutation.isPending ? "Quitando de la cola…" : "Quitar de la cola"}
            </button>
          ) : null}
          {archivedView ? (
            <button
              aria-label={"Restaurar " + job.chapterLabel + " de " + job.seriesTitle}
              className={styles.secondaryButton}
              disabled={restoreMutation.isPending}
              onClick={() => restoreMutation.mutate(job.jobId)}
              type="button"
            >
              {restoreMutation.isPending ? "Restaurando…" : "Restaurar en la cola"}
            </button>
          ) : null}
          {retryMutation.isSuccess ? (
            <p className={styles.successMessage} role="status">
              Se creó un nuevo intento; este fallo se conserva en el historial.
            </p>
          ) : null}
          {cancelMutation.isError ? (
            <JobActionError error={cancelMutation.error} title="No se pudo solicitar la cancelación" />
          ) : null}
          {retryMutation.isError ? (
            <JobActionError error={retryMutation.error} title="No se pudo reintentar la descarga" />
          ) : null}
          {archiveMutation.isError ? (
            <JobActionError error={archiveMutation.error} title="No se pudo quitar el trabajo de la cola" />
          ) : null}
          {restoreMutation.isError ? (
            <JobActionError error={restoreMutation.error} title="No se pudo restaurar el trabajo" />
          ) : null}
        </div>
      </article>
    </li>
  );
}

export function DownloadsPage() {
  const [offset, setOffset] = useState(0);
  const [archivedView, setArchivedView] = useState(false);
  const queryClient = useQueryClient();
  const jobsQuery = useQuery({
    queryKey: ["download-jobs", PAGE_SIZE, offset, archivedView],
    queryFn: () => getDownloadJobsPage({ limit: PAGE_SIZE, offset, archived: archivedView }),
  });
  const total = jobsQuery.data?.meta.total ?? 0;
  const states = jobsQuery.data?.meta.states;
  const queued = states?.QUEUED ?? 0;
  const active = [...cancellableStatuses].filter((status) => status !== "QUEUED")
    .reduce((count, status) => count + (states?.[status] ?? 0), 0);
  const pageNumber = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const updateJob = (updatedJob: DownloadJobSummary) => {
    queryClient.setQueryData<DownloadJobsPage>(
      ["download-jobs", PAGE_SIZE, offset, archivedView],
      (currentPage) =>
        currentPage === undefined
          ? currentPage
          : {
              ...currentPage,
              data: currentPage.data.map((job) =>
                job.jobId === updatedJob.jobId ? updatedJob : job,
              ),
            },
    );
  };
  const refreshRetriedJobs = () => {
    setOffset(0);
    void queryClient.invalidateQueries({ queryKey: ["download-jobs"] });
  };
  const refreshQueueAfterVisibilityChange = () => {
    setOffset(0);
    void queryClient.invalidateQueries({ queryKey: ["download-jobs"] });
  };

  return (
    <section aria-labelledby="downloads-title" className={styles.page}>
      <header className={styles.pageHeader}>
        <div className={styles.titleBlock}>
          <p className={styles.eyebrow}>Centro de descargas</p>
          <div className={styles.titleRow}>
            <h1 id="downloads-title">Descargas</h1>
            {jobsQuery.data ? (
              <span aria-live="polite" className={styles.queueCount} role="status">
                {states && !archivedView ? `${queued} pendientes · ${active} en curso`
                  : `${total} ${total === 1 ? "trabajo" : "trabajos"} ${archivedView ? (total === 1 ? "retirado" : "retirados") : (total === 1 ? "guardado" : "guardados")}`}
              </span>
            ) : null}
          </div>
          <p className={styles.intro}>
            Sigue el estado de tus capítulos y gestiona cada trabajo. El estado se actualiza automáticamente.
          </p>
        </div>
        <div className={styles.headerActions}>
          <button
            className={styles.secondaryButton}
            disabled={jobsQuery.isFetching}
            onClick={() => {
              setOffset(0);
              setArchivedView((current) => !current);
            }}
            type="button"
          >
            {archivedView ? "Volver a la cola" : "Ver trabajos retirados"}
          </button>
          <button
            className={styles.secondaryButton}
            disabled={jobsQuery.isFetching}
            onClick={() => void jobsQuery.refetch()}
            type="button"
          >
            {jobsQuery.isFetching ? "Actualizando…" : "Actualizar"}
          </button>
        </div>
      </header>
      {!archivedView && jobsQuery.data?.meta.recoveringJobId ? (
        <p className={styles.phaseMessage} role="status">
          Un capítulo falló. Yomira está buscando el mismo capítulo en otras fuentes,
          con el mismo manga, edición e idioma. La cola continuará al terminar la búsqueda.
        </p>
      ) : null}
      {!archivedView && (jobsQuery.data?.meta.waitingSources ?? 0) > 0 ? <p className={styles.phaseMessage} role="status">
        Una fuente no responde. Sus capítulos pendientes se conservan mientras se comprueba la conexión
        {jobsQuery.data?.meta.retryAfterSeconds ? ` de nuevo en ${jobsQuery.data.meta.retryAfterSeconds} segundos` : ""}.
        Puedes reintentar el capítulo fallido cuando vuelva a estar disponible.
      </p> : null}

      {states ? (
        <dl aria-label="Resumen de todas las descargas" className={styles.queueSummary}>
          {[["Pendientes", queued], ["En curso", active], ["Terminadas", states.COMPLETED ?? 0],
            ["Fallidas", states.FAILED ?? 0], ["Canceladas", states.CANCELLED ?? 0],
            ["Interrumpidas", states.INTERRUPTED ?? 0]].map(([label, count]) => (
            <div key={label}><dt>{label}</dt><dd>{count}</dd></div>
          ))}
        </dl>
      ) : null}

      <details className={styles.queueInfo}>
        <summary>Cómo se muestra el progreso y el historial</summary>
        <div>
          <p>
            Se descarga un capítulo a la vez, en orden numérico. Yomira termina los capítulos
            pendientes del manga actual antes de pasar al siguiente. El capítulo en curso y
            los pendientes aparecen primero; los trabajos terminados quedan debajo.
          </p>
          <p>
            Se muestra el conteo de páginas cuando Yomira dispone de ellas y las puede comprobar;
            no se muestra un porcentaje estimado. Algunas fuentes preparan primero las imágenes
            en Free Manga. Después se comprueban las páginas y se prepara el archivo.
            «Completado» indica que el capítulo está listo para leer.
          </p>
          <p>
            Si un capítulo de Free Manga falla, Yomira busca una coincidencia segura en otra
            fuente. Solo recupera ese capítulo y mantiene el orden del manga. El intento
            anterior queda en «Ver trabajos retirados». Si hay varias ediciones, capítulos
            especiales o ninguna coincidencia segura, el fallo queda visible para revisarlo.
          </p>
          <p>
            Retirar un trabajo terminado lo oculta de la cola y conserva el historial, las páginas
            y los archivos. Puedes restaurarlo desde «Ver trabajos retirados».
          </p>
        </div>
      </details>

      {jobsQuery.isPending ? (
        <p aria-live="polite" className={styles.statusMessage} role="status">
          Cargando trabajos guardados…
        </p>
      ) : jobsQuery.isError ? (
        <DownloadJobsError
          error={jobsQuery.error}
          onRetry={() => void jobsQuery.refetch()}
        />
      ) : jobsQuery.data.data.length === 0 ? (
        <div className={styles.emptyState}>
          <span aria-hidden="true" className={styles.emptyIcon}>⇩</span>
          <h2>{archivedView ? "No hay trabajos retirados" : "La cola está vacía"}</h2>
          <p className={styles.statusMessage} role="status">
            {archivedView
              ? "Los trabajos que retires de la cola aparecerán aquí y podrás restaurarlos."
              : "Busca una serie y añade capítulos; aquí verás su estado y podrás gestionarlos."}
          </p>
          {!archivedView ? (
            <Link className={styles.primaryButton} to="/search">Buscar mangas</Link>
          ) : null}
        </div>
      ) : (
        <>
          <div className={styles.listHeading}>
            <h2>{archivedView ? "Trabajos retirados" : "Trabajos guardados"}</h2>
            <p className={styles.statusMessage} role="status">
              {total} {total === 1 ? "trabajo" : "trabajos"}
            </p>
          </div>
          <ol aria-label="Trabajos de descarga guardados" className={styles.jobList}>
            {jobsQuery.data.data.map((job) => (
              <DownloadJobCard
                archivedView={archivedView}
                job={job}
                key={job.jobId}
                onJobUpdated={updateJob}
                onJobRetried={refreshRetriedJobs}
                onQueueChanged={refreshQueueAfterVisibilityChange}
                progress={jobsQuery.data.meta.pageProgress?.find((item) => item.jobId === job.jobId)}
              />
            ))}
          </ol>
          {pageCount > 1 ? (
            <nav aria-label="Paginación de trabajos" className={styles.pagination}>
              <button
                className={styles.secondaryButton}
                disabled={offset === 0 || jobsQuery.isFetching}
                onClick={() => setOffset((current) => Math.max(0, current - PAGE_SIZE))}
                type="button"
              >
                Anterior
              </button>
              <span aria-live="polite" className={styles.statusMessage}>
                Página {pageNumber} de {pageCount}
              </span>
              <button
                className={styles.secondaryButton}
                disabled={offset + PAGE_SIZE >= total || jobsQuery.isFetching}
                onClick={() => setOffset((current) => current + PAGE_SIZE)}
                type="button"
              >
                Siguiente
              </button>
            </nav>
          ) : null}
        </>
      )}
    </section>
  );
}
