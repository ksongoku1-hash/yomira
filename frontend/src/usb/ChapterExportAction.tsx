import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import {
  ApiError,
  createChapterUsbExport,
  getUsbExportTargets,
  type UsbExportJob,
} from "../api/client";
import styles from "./UsbExports.module.css";

export function ChapterExportAction({
  chapterId,
  chapterLabel,
  compact = false,
}: {
  chapterId: string;
  chapterLabel: string;
  compact?: boolean;
}) {
  const queryClient = useQueryClient();
  const [targetId, setTargetId] = useState("");
  const [message, setMessage] = useState("");
  const [resultJob, setResultJob] = useState<UsbExportJob | null>(null);
  const idempotencyKey = useRef<string | null>(null);
  const targetsQuery = useQuery({
    queryKey: ["usb-export-targets"],
    queryFn: getUsbExportTargets,
    staleTime: 5_000,
    retry: false,
  });
  const exportMutation = useMutation({
    mutationFn: async () => {
      if (!targetId) throw new Error("Selecciona una unidad USB o un lector conectado por cable.");
      idempotencyKey.current ??= globalThis.crypto.randomUUID();
      return createChapterUsbExport(chapterId, targetId, idempotencyKey.current);
    },
    onSuccess: (job) => {
      idempotencyKey.current = null;
      setResultJob(job);
      if (job.status === "SKIPPED") {
        setMessage("El CBZ ya estaba verificado en la unidad.");
      } else if (job.status === "EXPORTED") {
        setMessage("CBZ exportado y verificado.");
      } else if (job.status === "FAILED" || job.status === "INTERRUPTED") {
        setMessage("La exportación no terminó. Consulta su código y traza en el historial USB.");
      } else {
        setMessage("La exportación quedó pendiente; consulta el historial USB.");
      }
      void queryClient.invalidateQueries({ queryKey: ["usb-export-history"] });
    },
  });
  const targets = targetsQuery.data ?? [];
  const error = targetsQuery.error ?? exportMutation.error;
  const apiError = error instanceof ApiError ? error : null;
  if (compact && targets.length === 0) return null;

  return (
    <div className={styles.chapterExport}>
      {targetsQuery.isError ? (
        <div className={styles.inlineError}>
          <p aria-live="polite" role="alert">
            No se pudieron consultar los destinos USB.
            {apiError?.code ? <> Código: <code>{apiError.code}</code></> : null}
            {apiError?.traceId ? <> · Traza: <code>{apiError.traceId}</code></> : null}
          </p>
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
      {targetsQuery.isSuccess && targets.length === 0 ? (
        <div className={styles.inlineNotice}>
          <p>No se detectan unidades USB ni lectores conectados por cable.</p>
          <button
            className={styles.secondaryButton}
            disabled={targetsQuery.isFetching}
            onClick={() => void targetsQuery.refetch()}
            type="button"
          >
            Buscar unidades otra vez
          </button>
        </div>
      ) : null}
      {targets.length > 0 ? (
        <>
          <label className={styles.targetSelectLabel}>
            <span>Destino USB o lector para {chapterLabel}</span>
            <select
              disabled={targetsQuery.isFetching || exportMutation.isPending}
              onChange={(event) => {
                setTargetId(event.currentTarget.value);
                idempotencyKey.current = null;
                setMessage("");
                setResultJob(null);
              }}
              value={targetId}
            >
              <option value="">Selecciona una unidad</option>
              {targets.map((target) => (
                <option key={target.targetId} value={target.targetId}>
                  {target.displayName} · {target.fileSystem} · libres {formatBytes(target.freeBytes)}
                </option>
              ))}
            </select>
          </label>
          <button
            className={styles.exportButton}
            disabled={!targetId || targetsQuery.isFetching || exportMutation.isPending}
            onClick={() => {
              setMessage("");
              exportMutation.mutate();
            }}
            type="button"
          >
            {exportMutation.isPending ? "Exportando…" : `Exportar ${chapterLabel} a USB`}
          </button>
        </>
      ) : null}
      {exportMutation.isError ? (
        <p aria-live="assertive" className={styles.inlineError} role="alert">
          {apiError?.message ?? "No se pudo exportar el capítulo."}
          {apiError?.code ? <> Código: <code>{apiError.code}</code></> : null}
          {apiError?.traceId ? <> · Traza: <code>{apiError.traceId}</code></> : null}
        </p>
      ) : null}
      {message ? (
        <p
          aria-live={resultJob?.status === "FAILED" || resultJob?.status === "INTERRUPTED" ? "assertive" : "polite"}
          className={resultJob?.status === "FAILED" || resultJob?.status === "INTERRUPTED"
            ? styles.inlineError
            : styles.inlineSuccess}
          role={resultJob?.status === "FAILED" || resultJob?.status === "INTERRUPTED" ? "alert" : "status"}
        >
          {message}
          {resultJob && (resultJob.status === "FAILED" || resultJob.status === "INTERRUPTED") ? (
            <>
              {" Código: "}<code>{resultJob.errorCode ?? "Sin código de servidor"}</code>
              {" · Traza: "}<code>{resultJob.traceId}</code>
            </>
          ) : null}
        </p>
      ) : null}
    </div>
  );
}

function formatBytes(value: number): string {
  if (value < 1_000_000_000) return `${Math.floor(value / 1_000_000)} MB`;
  return `${(value / 1_000_000_000).toFixed(1)} GB`;
}
