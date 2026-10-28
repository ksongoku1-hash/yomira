import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { readerDiskCache } from "../api/readerCache";
import styles from "./SettingsPage.module.css";

const KEY = ["reader-disk-cache"] as const;
const mb = (bytes: number) => new Intl.NumberFormat("es", { maximumFractionDigits: 1 }).format(bytes / 1024 / 1024);
export function ReaderCachePanel() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: KEY, queryFn: () => readerDiskCache(), retry: false });
  const [draft, setDraft] = useState<string | null>(null);
  const save = useMutation({ mutationFn: readerDiskCache, onSuccess: (value) => { client.setQueryData(KEY, value); setDraft(null); } });
  const value = draft ?? String(query.data?.limitMb ?? "");
  const limit = Number(value);
  const valid = value.trim() !== "" && Number.isSafeInteger(limit) && limit >= 128 && limit <= 8192;
  return <section className={styles.appearanceCard} aria-labelledby="reader-disk-cache-title">
    <h2 id="reader-disk-cache-title">Caché de páginas online</h2>
    <p className={styles.fieldDescription}>Conserva temporalmente páginas ya leídas para abrirlas de nuevo más rápido. Tus descargas, marcadores y progreso se guardan por separado.</p>
    {query.isPending ? <p role="status">Consultando caché…</p> : query.isError ? <div role="alert">
      <p>No se pudo consultar la caché. La lectura sigue disponible.</p>
      <button className={styles.secondaryButton} type="button" onClick={() => void query.refetch()}>Reintentar caché</button>
    </div> : query.data ? <>
      <p>Imágenes almacenadas: {mb(query.data.usedBytes)} MB · {query.data.entries} páginas</p>
      <p className={styles.fieldDescription}>Archivo de caché: {mb(query.data.fileBytes)} MB. Al reducir el límite, el espacio del archivo se reutiliza; su tamaño puede tardar en disminuir.</p>
      <form onSubmit={(event) => { event.preventDefault(); if (valid && limit !== query.data?.limitMb) save.mutate(limit); }}>
        <label className={styles.fieldLabel} htmlFor="reader-disk-cache-limit">Límite en disco (MB)</label>
        <input className={styles.numberInput} id="reader-disk-cache-limit" type="number" min={128} max={8192} step={1}
          value={value} disabled={save.isPending} onChange={(event) => { setDraft(event.target.value); save.reset(); }} />
        <p className={styles.fieldDescription}>Entre 128 y 8192 MB. El límite de memoria del lector se configura por separado.</p>
        <button className={styles.primaryButton} type="submit" disabled={!valid || limit === query.data.limitMb || save.isPending}>
          {save.isPending ? "Guardando…" : "Guardar límite de caché"}</button>
        <button className={styles.secondaryButton} type="button" disabled={query.isFetching || save.isPending} onClick={() => void query.refetch()}>Actualizar uso</button>
        {save.isError ? <p role="alert">{save.error instanceof Error ? save.error.message : "No se pudo guardar el límite de caché."}</p>
          : save.isSuccess ? <p role="status">Límite de caché guardado.</p> : null}
      </form>
    </> : null}
  </section>;
}
