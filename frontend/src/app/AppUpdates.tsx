import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { appUpdates, getSystemVersion } from "../api/client";
import styles from "./AppUpdates.module.css";

const QUERY_KEY = ["app-updates"] as const;

export function AppUpdates({ compact = false, hidden = false }: { compact?: boolean; hidden?: boolean }) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => appUpdates(),
    staleTime: 1_000,
    refetchInterval: (current) => {
      const state = current.state.data?.state;
      return state === "unsupported" ? false : state === "checking" || state === "downloading" ? 1_000 : 60_000;
    },
  });
  const mutation = useMutation({
    mutationKey: QUERY_KEY,
    mutationFn: appUpdates,
    onSuccess: (status) => client.setQueryData(QUERY_KEY, status),
  });
  const [restartTimedOut, setRestartTimedOut] = useState(false);
  const status = query.data;
  const installing = status?.state === "installing";

  useEffect(() => {
    if (!installing) return;
    const started = Date.now();
    let active = true;
    let pending = false;
    const timer = window.setInterval(() => {
      if (Date.now() - started > 180_000) {
        setRestartTimedOut(true);
        window.clearInterval(timer);
        return;
      }
      if (pending) return;
      pending = true;
      void getSystemVersion().then((version) => {
        if (active && version.app_version === status.latest_version) window.location.reload();
      }).catch(() => undefined).finally(() => { pending = false; });
    }, 1_500);
    return () => { active = false; window.clearInterval(timer); };
  }, [installing, status?.latest_version]);

  if (hidden || (compact && !["downloading", "ready", "installing"].includes(status?.state ?? ""))) return null;
  const busy = mutation.isPending || ["checking", "downloading", "installing"].includes(status?.state ?? "");

  return <section aria-label="Actualizaciones de Yomira" className={`${styles.panel} ${compact ? styles.compact : ""}`}>
    <div>
      <h2>{compact ? `Yomira ${status?.latest_version ?? ""}` : "Actualizaciones de Yomira"}</h2>
      {!compact && status ? <p>Versión instalada: {status.current_version}</p> : null}
      <p role="status" aria-live="polite">
        {restartTimedOut ? "La actualización está tardando más de lo esperado. Abre Yomira desde su acceso directo para comprobar el resultado."
          : installing ? "Instalando la actualización. Yomira volverá a abrirse automáticamente…"
          : status?.message || (query.isPending ? "Comprobando actualizaciones…" : "Las actualizaciones se comprueban al iniciar y cada seis horas.")}
      </p>
      {status?.state === "downloading" && status.total_bytes > 0 ?
        <progress aria-label="Descarga de la actualización" max={status.total_bytes} value={Math.min(status.downloaded_bytes, status.total_bytes)} /> : null}
      {mutation.isError ? <p role="alert">No se pudo completar la operación. Vuelve a intentarlo.</p> : null}
      {!compact && query.isError ? <p role="alert">No se pudo consultar el estado de las actualizaciones.</p> : null}
    </div>
    <div className={styles.actions}>
      {status?.state === "ready" ? <button disabled={busy} onClick={() => mutation.mutate("install")} type="button">
        {mutation.isPending ? "Preparando…" : "Reiniciar y actualizar"}
      </button> : null}
      {!compact && status?.state !== "unsupported" && status?.state !== "ready" ?
        <button disabled={busy} onClick={() => mutation.mutate("check")} type="button">{busy ? "Actualizando…" : "Buscar actualizaciones"}</button> : null}
      {!compact ? <a href="https://github.com/ksongoku1-hash/yomira/releases" target="_blank" rel="noreferrer">Ver versiones en GitHub</a> : null}
    </div>
  </section>;
}
