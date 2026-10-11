import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import {
  ApiError,
  getUsbExportHistoryPage,
  getUsbExportTargets,
  createUsbAssetExport,
  type UsbExportJob,
  type UsbExportStatus,
} from "../api/client";
import styles from "./UsbExports.module.css";
import { ExportProgress } from "./ExportProgress";

const HISTORY_PAGE_SIZE = 50;

const statusLabels: Record<UsbExportStatus, string> = {
  QUEUED: "En cola",
  EXPORTING: "Exportando",
  EXPORTED: "Exportado y verificado",
  SKIPPED: "Ya estaba verificado",
  FAILED: "Fallido",
  INTERRUPTED: "Interrumpido",
};

export function UsbExportsPage() {
  const [retrying, setRetrying] = useState(false);
  const targetsQuery = useQuery({
    queryKey: ["usb-export-targets"],
    queryFn: getUsbExportTargets,
    staleTime: 5_000,
    retry: false,
  });
  const historyQuery = useInfiniteQuery({
    queryKey: ["usb-export-history"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      getUsbExportHistoryPage({ limit: HISTORY_PAGE_SIZE, cursor: pageParam }),
    getNextPageParam: (lastPage) => lastPage.meta.nextCursor ?? undefined,
    refetchInterval: (query) => retrying || query.state.data?.pages.some((page) =>
      page.data.some((job) => job.status === "EXPORTING" || job.status === "QUEUED")) ? 2_000 : false,
    retry: false,
  });
  const manualRefresh = useMutation({
    mutationFn: () => Promise.all([historyQuery.refetch(), targetsQuery.refetch()]),
  });
  const jobs = historyQuery.data?.pages.flatMap((page) => page.data) ?? [];
  const total = historyQuery.data?.pages[historyQuery.data.pages.length - 1]?.meta.total ?? 0;
  const historyError = historyQuery.error instanceof ApiError ? historyQuery.error : null;
  const targetsError = targetsQuery.error instanceof ApiError ? targetsQuery.error : null;

  return (
    <section aria-busy={historyQuery.isPending || targetsQuery.isPending || manualRefresh.isPending || historyQuery.isFetchingNextPage} className={styles.page}>
      <header className={styles.pageHeader}>
        <div>
          <h1>Exportaciones USB</h1>
          <p className={styles.intro}>
            Exporta un manga desde su ficha en un solo archivo. El lector lo recibe en Books; aquí puedes revisar el avance y reintentar un envío.
          </p>
        </div>
      </header>

      <section aria-labelledby="usb-targets-title" className={styles.targetPanel}>
        <h2 id="usb-targets-title">Unidades USB y lectores conectados</h2>
        {targetsQuery.isPending ? <p className={styles.statusMessage}>Consultando destinos…</p> : null}
        {targetsQuery.isError ? (
          <ErrorMessage error={targetsError} fallback="No se pudieron consultar las unidades USB." />
        ) : null}
        {targetsQuery.isSuccess && targetsQuery.data.length === 0 ? (
          <p className={styles.statusMessage}>No se detectan unidades USB ni lectores conectados por cable.</p>
        ) : null}
        {targetsQuery.data && targetsQuery.data.length > 0 ? (
          <ul className={styles.targetList}>
            {targetsQuery.data.map((target) => (
              <li key={target.targetId}>
                <strong>{target.displayName}</strong>
                <span>{target.fileSystem}</span>
                <span>{formatBytes(target.freeBytes)} libres de {formatBytes(target.totalBytes)}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <section aria-labelledby="usb-history-title" className={styles.historyPanel}>
        <header>
          <div>
            <h2 id="usb-history-title">Historial local</h2>
            <p className={styles.statusMessage}>
              {jobs.length} {jobs.length === 1 ? "registro cargado" : "registros cargados"}
              {total > jobs.length ? ` · conteo más reciente de la base: ${total}` : ""}
            </p>
          </div>
          <button
            className={styles.secondaryButton}
            disabled={historyQuery.isPending || targetsQuery.isPending || manualRefresh.isPending}
            onClick={() => manualRefresh.mutate()}
            type="button"
          >
            Actualizar datos
          </button>
        </header>

        {historyQuery.isPending ? <p aria-live="polite" className={styles.statusMessage} role="status">Cargando historial…</p> : null}
        {historyQuery.isError ? (
          <ErrorMessage error={historyError} fallback="No se pudo cargar el historial de exportaciones." />
        ) : null}
        {historyQuery.isSuccess && jobs.length === 0 ? (
          <div className={styles.emptyState}>
            <p>No hay exportaciones registradas todavía. Abre una serie y elige Exportar manga a USB o lector conectado.</p>
          </div>
        ) : null}
        {jobs.length > 0 ? (
          <ul aria-label="Exportaciones USB guardadas" className={styles.historyList}>
            {jobs.map((job) => <ExportHistoryItem job={job} key={job.exportId}
              recovered={jobs.some((item) => item.assetId === job.assetId && item.targetId === job.targetId &&
                (item.status === "EXPORTED" || item.status === "SKIPPED") && Date.parse(item.createdAt) > Date.parse(job.createdAt))}
              blocked={retrying || jobs.some((item) => item.status === "EXPORTING" || item.status === "QUEUED")}
              onRetrying={setRetrying} />)}
          </ul>
        ) : null}
        {historyQuery.hasNextPage ? (
          <button
            className={`${styles.secondaryButton} ${styles.loadMore}`}
            disabled={historyQuery.isFetchingNextPage}
            onClick={() => void historyQuery.fetchNextPage()}
            type="button"
          >
            {historyQuery.isFetchingNextPage ? "Cargando…" : "Cargar más"}
          </button>
        ) : null}
      </section>
    </section>
  );
}

function ExportHistoryItem({ job, blocked, recovered, onRetrying }: {
  job: UsbExportJob; blocked: boolean; recovered: boolean; onRetrying: (value: boolean) => void;
}) {
  const client = useQueryClient();
  const requestKey = useRef<string | null>(null);
  const retry = useMutation({
    mutationFn: () => {
      requestKey.current ??= globalThis.crypto.randomUUID();
      return createUsbAssetExport(job.assetId, job.targetId, requestKey.current);
    },
    onMutate: () => onRetrying(true),
    onSuccess: () => { requestKey.current = null; },
    onSettled: () => {
      onRetrying(false);
      void client.invalidateQueries({ queryKey: ["usb-export-history"] });
      void client.invalidateQueries({ queryKey: ["usb-export-targets"] });
    },
    retry: false,
  });
  return (
    <li className={styles.historyItem}>
      <h3>{job.destinationName}</h3>
      <div className={styles.historyMeta}>
        <span>{statusLabels[job.status]}</span>
        <span>{job.targetLabel} · {job.fileSystem}</span>
        <span>{formatBytes(job.expectedSizeBytes)}</span>
        <time dateTime={job.createdAt}>{new Date(job.createdAt).toLocaleString()}</time>
      </div>
      <ExportProgress job={job} />
      {job.status === "FAILED" || job.status === "INTERRUPTED" ? (
        <p className={styles.historyError}>
          {job.errorCode ?? "Error sin código"} · traza <code>{job.traceId}</code>
          {job.failureStage ? ` · etapa ${job.failureStage}` : ""}
        </p>
      ) : null}
      {(job.status === "FAILED" || job.status === "INTERRUPTED") && recovered
        ? <p>Resuelto por un envío posterior verificado.</p> : null}
      {(job.status === "FAILED" || job.status === "INTERRUPTED") && !recovered ? <div>
        <p>{job.errorCode === "EXP_DEVICE_DISCONNECTED"
          ? "Enciende y desbloquea el lector, vuelve a conectarlo y activa la transferencia de archivos."
          : "Se comprobará la copia anterior antes de volver a enviar el archivo."}</p>
        <button className={styles.secondaryButton} type="button" disabled={blocked || retry.isPending}
          onClick={() => retry.mutate()} aria-label={`Reintentar envío de ${job.destinationName}`}>
          {retry.isPending ? "Reintentando envío…" : "Reintentar envío"}
        </button>
        {retry.isError ? <ErrorMessage error={retry.error instanceof ApiError ? retry.error : null}
          fallback="No se pudo solicitar el reintento." /> : null}
        {retry.isSuccess ? <p role="status">{retry.data.status === "EXPORTED" || retry.data.status === "SKIPPED"
          ? "Envío comprobado en el destino." : "Consulta el nuevo intento en el historial."}</p> : null}
      </div> : null}
    </li>
  );
}

function ErrorMessage({ error, fallback }: { error: ApiError | null; fallback: string }) {
  return (
    <div className={styles.errorState} role="alert">
      <p>{error?.message ?? fallback}</p>
      <p className={styles.errorDetails}>
        Código: <code>{error?.code ?? "Sin código de servidor"}</code>
        {error?.status ? ` · HTTP ${error.status}` : ""}
        {error?.traceId ? <> · Traza: <code>{error.traceId}</code></> : null}
      </p>
    </div>
  );
}

function formatBytes(value: number): string {
  if (value < 1_000_000_000) return `${Math.floor(value / 1_000_000)} MB`;
  return `${(value / 1_000_000_000).toFixed(1)} GB`;
}
