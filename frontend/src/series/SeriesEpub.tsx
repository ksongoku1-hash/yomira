import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, createSeriesEpub, getSeriesEpubFileUrl, getSeriesEpubStatus } from "../api/client";
import { ActionIcon } from "../app/ActionIcon";
import { useState } from "react";
import { EpubUsbExport } from "./EpubUsbExport";
import styles from "./SeriesDetailPage.module.css";

export function SeriesEpub({ seriesId, localChapterCount }: { seriesId: string; localChapterCount: number }) {
  const client = useQueryClient();
  const [sending, setSending] = useState(false);
  const queryKey = ["series-epub", seriesId, localChapterCount] as const;
  const query = useQuery({ queryKey, queryFn: () => getSeriesEpubStatus(seriesId), retry: false,
    refetchInterval: (current) => current.state.status !== "error" && current.state.data?.status === "building" ? 2_000 : false });
  const build = useMutation({ mutationFn: () => createSeriesEpub(seriesId), retry: false,
    onMutate: () => client.cancelQueries({ queryKey }),
    onSuccess: (result) => { client.setQueryData(queryKey, result); } });
  const book = query.data;
  const busy = build.isPending || book?.status === "building";
  const error = build.error ?? (book?.status === "failed" ? book.error || "No se pudo preparar el EPUB. Reintenta cuando quieras." : null);

  return <section className={styles.cbrExport} aria-labelledby="series-epub-title">
    <header className={styles.chaptersHeader}><h2 id="series-epub-title">Tu libro EPUB</h2></header>
    <p className={styles.statusMessage}>Un EPUB por manga con todos sus capítulos descargados, también los de otras páginas de la lista. Se crea o actualiza solo cuando tú lo decides.</p>
    <p className={styles.statusMessage}>Preparado para lectores con imágenes JPEG y PNG. Las páginas WebP se convierten a PNG conservando los píxeles; el libro puede ocupar más espacio.</p>
    {localChapterCount === 0 ? <p className={styles.statusMessage}>Descarga al menos un capítulo para crear el libro.</p> : null}
    {query.isPending ? <p role="status" className={styles.statusMessage}>Consultando el EPUB…</p> : null}
    {book?.status === "outdated" && !busy ? <p role="status">Hay nuevos capítulos o cambios de contenido o compatibilidad. Actualiza el EPUB para guardar la versión actual del libro.</p> : null}
    {book?.hasFile ? <div className={styles.cbrResult}>
      <p>{book.filename ?? "Libro EPUB"}{book.sizeBytes !== null ? ` · ${(book.sizeBytes / 1_048_576).toLocaleString("es", { maximumFractionDigits: 1 })} MB` : ""}</p>
      {book.status === "ready" ? <p role="status">EPUB listo: {book.chapterCount} capítulos · {book.pageCount} páginas.</p> :
        <p>El EPUB anterior sigue disponible mientras preparas su actualización.</p>}
      <a className={styles.secondaryButton} href={getSeriesEpubFileUrl(seriesId)} download><ActionIcon name="download" />Guardar EPUB</a>
      <EpubUsbExport key={seriesId} seriesId={seriesId} disabled={busy} onBusyChange={setSending} />
    </div> : null}
    {busy ? <div className={styles.cbrResult}>
      <p role="status">{book?.status === "building" && book.pageCount > 0 ? `Creando EPUB… ${book.completedPages} de ${book.pageCount} páginas.` : "Preparando capítulos del EPUB…"}</p>
      <progress aria-label="Progreso del EPUB" max={book?.pageCount || 1}
        value={book?.status === "building" && book.pageCount > 0 ? book.completedPages : undefined} />
    </div> : null}
    {error && !busy ? <div className={styles.cbrError} role="alert"><p>{typeof error === "string" ? error : error instanceof ApiError ? error.message : "No se pudo iniciar el EPUB. Reintenta cuando quieras."}</p>
      {book?.hasFile ? <p>Puedes guardar el EPUB anterior.</p> : null}</div> : null}
    {query.isError ? <div className={styles.cbrError}>
      <p role="status">No se pudo consultar el EPUB. Puedes volver a comprobarlo.</p>
      <button type="button" className={styles.secondaryButton} disabled={query.isFetching} onClick={() => void query.refetch()}>Comprobar EPUB</button>
    </div> : null}
    <button type="button" className={styles.secondaryButton} disabled={localChapterCount < 1 || busy || sending || query.isPending || query.isError}
      onClick={() => build.mutate()}><ActionIcon name="device" />{busy ? "Creando EPUB…" : book?.hasFile ? "Actualizar EPUB" : "Crear EPUB"}</button>
  </section>;
}
