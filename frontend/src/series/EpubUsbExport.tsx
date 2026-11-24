import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ApiError, createUsbAssetExport, getUsbExportHistoryPage, getUsbExportTargets,
  prepareSeriesEpubUsbAsset } from "../api/client";
import { isMobileConnection } from "../settings/MobileAccessPanel";
import { ExportProgress } from "../usb/ExportProgress";
import styles from "./SeriesDetailPage.module.css";

export function EpubUsbExport({ seriesId, disabled, onBusyChange }: {
  seriesId: string; disabled: boolean; onBusyChange: (busy: boolean) => void;
}) {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [targetId, setTargetId] = useState("");
  const [assetId, setAssetId] = useState<string | null>(null);
  const key = useRef<string | null>(null);
  const targets = useQuery({ queryKey: ["usb-export-targets"], queryFn: getUsbExportTargets,
    enabled: open && !isMobileConnection, staleTime: 5_000, retry: false });
  const send = useMutation({
    mutationFn: async () => {
      const asset = await prepareSeriesEpubUsbAsset(seriesId);
      setAssetId(asset);
      key.current ??= crypto.randomUUID();
      return createUsbAssetExport(asset, targetId, key.current);
    },
    retry: false,
    onMutate: () => onBusyChange(true),
    onSuccess: () => {
      key.current = null;
      void client.invalidateQueries({ queryKey: ["usb-export-history"] });
      void client.invalidateQueries({ queryKey: ["usb-export-targets"] });
    },
    onSettled: () => onBusyChange(false),
  });
  const history = useQuery({ queryKey: ["usb-export-history", "active"],
    queryFn: () => getUsbExportHistoryPage({ limit: 50, cursor: null }),
    enabled: send.isPending && assetId !== null, refetchInterval: send.isPending ? 2_000 : false, retry: false });
  const active = history.data?.data.find(job => job.assetId === assetId && job.targetId === targetId && job.status === "EXPORTING");
  const job = send.data;
  if (isMobileConnection) return null;

  return <details className={styles.cbrResult} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Enviar al lector</summary>
    {open ? <>
      <p>Conecta el lector por cable y activa la transferencia de archivos. La copia se comprueba antes de dar el envío por terminado.</p>
      <button type="button" className={styles.secondaryButton} disabled={targets.isFetching || send.isPending}
        onClick={() => void targets.refetch()}>{targets.isFetching ? "Buscando lectores…" : "Detectar lector"}</button>
      {targets.isError ? <p role="alert">No se pudieron consultar los lectores. Vuelve a detectar el lector.</p> : null}
      {targets.isSuccess && targets.data.length === 0 ? <p role="status">No se detectan lectores ni unidades USB. Comprueba el cable y la transferencia de archivos.</p> : null}
      {targets.data && targets.data.length > 0 ? <div className={styles.cbrUsbAction}>
        <label className={styles.cbrTargetField}>Lector o unidad USB
          <select value={targetId} disabled={send.isPending} onChange={event => {
            setTargetId(event.currentTarget.value); key.current = null; setAssetId(null); send.reset();
          }}>
            <option value="">Selecciona un destino</option>
            {targets.data.map(target => <option key={target.targetId} value={target.targetId}>
              {target.displayName} · {(target.freeBytes / 1024 ** 3).toLocaleString("es", { maximumFractionDigits: 1 })} GB libres
            </option>)}
          </select>
        </label>
        <button type="button" className={styles.secondaryButton} disabled={disabled || !targetId || send.isPending || targets.isFetching}
          onClick={() => send.mutate()}>{send.isPending ? "Enviando EPUB…" : "Enviar EPUB"}</button>
      </div> : null}
      {send.isPending ? active ? <ExportProgress job={active} /> : <p role="status">Comprobando el EPUB y preparando el envío…</p> : null}
      {send.isError ? <p role="alert">{send.error instanceof ApiError ? send.error.message : "No se pudo enviar el EPUB. Comprueba la conexión y reintenta."}</p> : null}
      {job ? <p role={job.status === "FAILED" || job.status === "INTERRUPTED" ? "alert" : "status"}>
        {job.status === "EXPORTED" ? "EPUB enviado y verificado. Ya puedes abrirlo desde la biblioteca del lector." :
          job.status === "SKIPPED" ? "Este EPUB ya estaba verificado en el lector." :
          job.status === "FAILED" || job.status === "INTERRUPTED" ? `No se completó el envío. Código: ${job.errorCode ?? "sin código"} · Traza: ${job.traceId}. Puedes reintentar; revisa también el historial USB.` :
          "El envío sigue en el historial USB."}
      </p> : null}
    </> : null}
  </details>;
}
