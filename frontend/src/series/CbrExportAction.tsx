import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import {
  ApiError,
  createCbrExport,
  createUsbAssetExport,
  getUsbExportTargets,
  getUsbExportHistoryPage,
  type CbrExportResult,
  type UsbExportJob,
} from "../api/client";
import styles from "./SeriesDetailPage.module.css";
import { ExportProgress } from "../usb/ExportProgress";

interface CbrSelection {
  key: string;
  seriesId: string;
  chapterIds: string[];
}

interface UsbSelection {
  key: string;
  assetId: string;
  targetId: string;
}

export function CbrExportAction({
  seriesId,
  chapterIds,
  pageCount,
}: {
  seriesId: string;
  chapterIds: string[];
  pageCount: number;
}) {
  const queryClient = useQueryClient();
  const selectionKey = [...chapterIds].sort().join(":");
  const [builtAsset, setBuiltAsset] = useState<{
    key: string;
    result: CbrExportResult;
  } | null>(null);
  const [targetId, setTargetId] = useState("");
  const idempotency = useRef<{ assetId: string; targetId: string; key: string } | null>(null);
  const currentResult = builtAsset?.key === selectionKey ? builtAsset.result : null;
  const targetsQuery = useQuery({
    queryKey: ["usb-export-targets"],
    queryFn: getUsbExportTargets,
    enabled: currentResult !== null,
    staleTime: 5_000,
    retry: false,
  });
  const buildMutation = useMutation({
    mutationFn: ({ seriesId: requestedSeries, chapterIds: requestedChapters }: CbrSelection) =>
      createCbrExport(requestedSeries, requestedChapters),
    retry: false,
    onSuccess: (result, selection) => {
      setBuiltAsset({ key: selection.key, result });
      setTargetId("");
      idempotency.current = null;
      void queryClient.invalidateQueries({ queryKey: ["usb-export-targets"] });
    },
  });
  const exportMutation = useMutation({
    mutationFn: async ({ assetId, targetId: requestedTarget }: UsbSelection) => {
      let requestKey = idempotency.current;
      if (requestKey?.assetId !== assetId || requestKey.targetId !== requestedTarget) {
        requestKey = { assetId, targetId: requestedTarget, key: globalThis.crypto.randomUUID() };
        idempotency.current = requestKey;
      }
      return createUsbAssetExport(assetId, requestedTarget, requestKey.key);
    },
    retry: false,
    onSuccess: (job) => {
      idempotency.current = null;
      void queryClient.invalidateQueries({ queryKey: ["usb-export-history"] });
      void queryClient.invalidateQueries({ queryKey: ["usb-export-targets"] });
      return job;
    },
  });

  const activeHistory = useQuery({
    queryKey: ["usb-export-history", "active"],
    queryFn: () => getUsbExportHistoryPage({ limit: 50, cursor: null }),
    enabled: exportMutation.isPending,
    refetchInterval: exportMutation.isPending ? 2_000 : false,
    retry: false,
  });
  const activeJob = activeHistory.data?.data.find((job) =>
    job.assetId === currentResult?.assetId && job.targetId === targetId && job.status === "EXPORTING");

  const currentBuildError = buildMutation.isError &&
    buildMutation.variables?.key === selectionKey
    ? buildMutation.error
    : null;
  const currentExportJob: UsbExportJob | null = exportMutation.isSuccess &&
    exportMutation.variables?.key === selectionKey &&
    exportMutation.variables.assetId === currentResult?.assetId
    ? exportMutation.data
    : null;
  const currentExportError = exportMutation.isError &&
    exportMutation.variables?.key === selectionKey &&
    exportMutation.variables.assetId === currentResult?.assetId
    ? exportMutation.error
    : null;

  function build() {
    if (chapterIds.length === 0 || pageCount < 1 || pageCount > 10_000) return;
    buildMutation.mutate({ key: selectionKey, seriesId, chapterIds: [...chapterIds] });
  }

  function exportToUsb() {
    if (currentResult === null || !targetId) return;
    exportMutation.mutate({
      key: selectionKey,
      assetId: currentResult.assetId,
      targetId,
    });
  }

  return (
    <section aria-labelledby="cbr-export-title" className={styles.cbrExport}>
      <div>
        <h3 id="cbr-export-title">Crear un CBR para lector</h3>
        <p className={styles.statusMessage}>
          {chapterIds.length} {chapterIds.length === 1 ? "capítulo" : "capítulos"} · {pageCount} páginas.
          {" "}Los CBZ originales se conservan. La lectura de Vinland Saga en CBR está comprobada en el B6 Color.
        </p>
      </div>
      <button
        className={styles.secondaryButton}
        disabled={chapterIds.length === 0 || pageCount < 1 || pageCount > 10_000 || buildMutation.isPending}
        onClick={build}
        type="button"
      >
        {buildMutation.isPending
          ? "Creando y verificando CBR…"
          : "Crear CBR (" + chapterIds.length + " " +
            (chapterIds.length === 1 ? "capítulo)" : "capítulos)")}
      </button>
      {currentBuildError !== null ? (
        <div className={styles.cbrError} role="alert">
          <p>{currentBuildError instanceof ApiError
            ? currentBuildError.message
            : "No se pudo crear el CBR."}</p>
          {currentBuildError instanceof ApiError ? (
            <p>
              Código: <code>{currentBuildError.code ?? "Sin código de servidor"}</code>
              {currentBuildError.traceId ? <> · Traza: <code>{currentBuildError.traceId}</code></> : null}
            </p>
          ) : null}
          <button className={styles.secondaryButton} onClick={build} type="button">
            Reintentar creación
          </button>
        </div>
      ) : null}
      {currentResult !== null ? (
        <div className={styles.cbrResult} role="status">
          <p>
            CBR creado y verificado: <strong>{currentResult.filename}</strong>
            {" · "}{currentResult.pageCount} páginas · {formatBytes(currentResult.sizeBytes)}
          </p>
          {targetsQuery.isPending ? <p>Buscando unidades USB y lectores conectados…</p> : null}
          {targetsQuery.isError ? (
            <div className={styles.cbrError} role="alert">
              <p>No se pudieron consultar los destinos USB.</p>
              <button
                className={styles.secondaryButton}
                disabled={targetsQuery.isFetching}
                onClick={() => void targetsQuery.refetch()}
                type="button"
              >
                Volver a consultar unidades
              </button>
            </div>
          ) : null}
          {targetsQuery.isSuccess && targetsQuery.data.length === 0 ? (
            <p>No se detectan unidades USB ni lectores conectados por cable.</p>
          ) : null}
          {targetsQuery.data && targetsQuery.data.length > 0 ? (
            <div className={styles.cbrUsbAction}>
              <label className={styles.cbrTargetField}>
                <span>Destino USB o lector</span>
                <select
                  disabled={targetsQuery.isFetching || exportMutation.isPending}
                  onChange={(event) => {
                    setTargetId(event.currentTarget.value);
                    idempotency.current = null;
                    exportMutation.reset();
                  }}
                  value={targetId}
                >
                  <option value="">Selecciona un destino</option>
                  {targetsQuery.data.map((target) => (
                    <option key={target.targetId} value={target.targetId}>
                      {target.displayName} · {target.fileSystem} · libres {formatBytes(target.freeBytes)}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className={styles.secondaryButton}
                disabled={!targetId || targetsQuery.isFetching || exportMutation.isPending}
                onClick={exportToUsb}
                type="button"
              >
                {exportMutation.isPending ? "Exportando CBR…" : "Exportar CBR a USB"}
              </button>
            </div>
          ) : null}
          {currentExportError !== null ? (
            <div className={styles.cbrError} role="alert">
              <p>{currentExportError instanceof ApiError
                ? currentExportError.message
                : "No se pudo exportar el CBR."}</p>
              {currentExportError instanceof ApiError ? (
                <p>
                  Código: <code>{currentExportError.code ?? "Sin código de servidor"}</code>
                  {currentExportError.traceId ? <> · Traza: <code>{currentExportError.traceId}</code></> : null}
                </p>
              ) : null}
              <button className={styles.secondaryButton} onClick={exportToUsb} type="button">
                Reintentar exportación
              </button>
            </div>
          ) : null}
          {exportMutation.isPending && activeJob ? <ExportProgress job={activeJob} /> : null}
          {currentExportJob !== null ? (
            <p className={currentExportJob.status === "FAILED" ? styles.cbrError : styles.statusMessage}>
              {currentExportJob.status === "EXPORTED"
                ? "CBR exportado y verificado."
                : currentExportJob.status === "SKIPPED"
                  ? "El CBR ya estaba verificado en el destino."
                  : currentExportJob.status === "FAILED" || currentExportJob.status === "INTERRUPTED"
                    ? "La exportación falló: " + (currentExportJob.errorCode ?? "sin código") +
                      " · Traza " + currentExportJob.traceId
                    : "La exportación quedó pendiente; consulta el historial USB."}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function formatBytes(value: number): string {
  if (value < 1_000_000_000) return Math.floor(value / 1_000_000) + " MB";
  return (value / 1_000_000_000).toFixed(1) + " GB";
}
