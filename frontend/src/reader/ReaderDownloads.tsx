import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { getChapterAvailability, getConnectorChapters, getReaderDownloadSource,
  queueConnectorChapters } from "../api/client";
import styles from "./ReaderPage.module.css";

const PAGE_SIZE = 100;

export function ReaderDownloads({ chapterId, open, onOpenChange }: {
  chapterId: string; open: boolean; onOpenChange: (open: boolean) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [offset, setOffset] = useState(0);
  const client = useQueryClient();
  const sourceQuery = useQuery({ queryKey: ["reader-download-source", chapterId],
    queryFn: () => getReaderDownloadSource(chapterId), enabled: open, retry: false });
  const source = sourceQuery.data;
  const chaptersQuery = useQuery({
    queryKey: ["connector-chapters", source?.connectorId, source?.remoteSeriesId],
    queryFn: () => getConnectorChapters(source!.connectorId, source!.remoteSeriesId),
    enabled: open && source != null, staleTime: 60_000, retry: false,
  });
  const availability = useQuery({
    queryKey: ["chapter-availability", source?.connectorId, source?.remoteSeriesId],
    queryFn: () => getChapterAvailability(source!.connectorId, source!.remoteSeriesId),
    enabled: open && source != null, refetchInterval: open ? 5_000 : false, retry: false,
  });
  const chapters = chaptersQuery.data?.data ?? [];
  const ids = [...new Set(chapters.flatMap((chapter) => chapter.remoteId === null ? [] : [chapter.remoteId]))];
  const ready = new Set(availability.data?.readyRemoteIds ?? []);
  const busy = new Set(availability.data?.busyRemoteIds ?? []);
  const pendingIds = ids.filter((id) => !ready.has(id) && !busy.has(id));
  const selectedSet = new Set(selected);
  const selectedIds = pendingIds.filter((id) => selectedSet.has(id));
  const download = useMutation({
    mutationFn: (chapterIds: string[]) => {
      if (source == null) throw new Error("La fuente no está disponible.");
      return queueConnectorChapters(source.connectorId, source.remoteSeriesId, chapterIds);
    },
    retry: false,
    onSettled: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["download-jobs"] }),
        client.invalidateQueries({ queryKey: ["chapter-availability", source?.connectorId, source?.remoteSeriesId] }),
        client.invalidateQueries({ queryKey: ["library"] }),
      ]);
    },
  });
  const currentReady = source != null && availability.data?.readyRemoteIds.includes(source.chapterRemoteId);
  const currentBusy = source != null && availability.data?.busyRemoteIds.includes(source.chapterRemoteId);
  const canDownload = chaptersQuery.isSuccess && availability.isSuccess && pendingIds.length > 0 && !download.isPending;

  return <section className={styles.downloadSection} aria-label="Descargas del lector">
    <button className={styles.secondaryButton} type="button" aria-expanded={open}
      aria-controls="reader-downloads" onClick={() => {
        onOpenChange(!open);
      }}>Descargar</button>
    {open ? <div id="reader-downloads" className={styles.downloadPanel}>
      <h2>Guardar para leer sin conexión</h2>
      <p>La lectura online sigue sin guardar páginas. Solo estas descargas ocupan espacio en disco.</p>
      {sourceQuery.isPending ? <p role="status">Buscando la fuente del capítulo…</p> : null}
      {sourceQuery.isError ? <p role="alert">No se pudo consultar la fuente.
        <button type="button" className={styles.secondaryButton} onClick={() => void sourceQuery.refetch()}>Reintentar fuente</button>
      </p> : null}
      {source === null ? <p role="status">Este capítulo no tiene una fuente de descarga disponible. Puedes seguir leyendo tu copia local.</p> : null}
      {source != null ? <>
        {chaptersQuery.isPending ? <p role="status">Cargando capítulos…</p> : null}
        {chaptersQuery.isError ? <p role="alert">No se pudo cargar la lista de capítulos.
          <button type="button" className={styles.secondaryButton} onClick={() => void chaptersQuery.refetch()}>Reintentar capítulos</button>
        </p> : null}
        {availability.isError ? <p role="alert">No se pudo comprobar qué capítulos están guardados o en cola.
          <button type="button" className={styles.secondaryButton} onClick={() => void availability.refetch()}>Reintentar estados</button>
        </p> : null}
        <div className={styles.readerTools}>
          <button type="button" className={styles.secondaryButton}
            disabled={!canDownload || !ids.includes(source.chapterRemoteId) || !!currentReady || !!currentBusy}
            onClick={() => download.mutate([source.chapterRemoteId])}>
            {currentReady ? "Capítulo descargado" : currentBusy ? "Capítulo en cola" : "Descargar capítulo actual"}
          </button>
          {selectedIds.length > 0 ? <button type="button" className={styles.secondaryButton}
            disabled={!canDownload || selectedIds.length === 0}
            onClick={() => download.mutate(selectedIds)}>Descargar selección ({selectedIds.length})</button> : null}
          <button type="button" className={styles.secondaryButton} disabled={!canDownload}
            onClick={() => download.mutate(pendingIds)}>Descargar manga completo</button>
        </div>
        <ul className={styles.downloadChapterList}>
          {chapters.slice(offset, offset + PAGE_SIZE).map((chapter, index) => <li key={`${chapter.remoteId}:${offset + index}`}>
            <label><input aria-label={chapter.displayLabel} type="checkbox" disabled={!availability.isSuccess || chapter.remoteId === null || ready.has(chapter.remoteId) || busy.has(chapter.remoteId) || download.isPending}
              checked={chapter.remoteId !== null && selectedSet.has(chapter.remoteId)}
              onChange={(event) => {
                const id = chapter.remoteId;
                if (id !== null) setSelected((current) => event.target.checked
                  ? [...new Set([...current, id])] : current.filter((value) => value !== id));
              }} />{chapter.displayLabel}{chapter.remoteId !== null && ready.has(chapter.remoteId) ? <small> · Guardado</small> : chapter.remoteId !== null && busy.has(chapter.remoteId) ? <small> · En cola</small> : null}</label>
          </li>)}
        </ul>
        {chapters.length > PAGE_SIZE ? <nav className={styles.readerTools} aria-label="Lista de capítulos para descargar">
          <button type="button" className={styles.secondaryButton} disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Capítulos anteriores</button>
          <span>{offset + 1}–{Math.min(offset + PAGE_SIZE, chapters.length)} de {chapters.length}</span>
          <button type="button" className={styles.secondaryButton} disabled={offset + PAGE_SIZE >= chapters.length}
            onClick={() => setOffset(offset + PAGE_SIZE)}>Más capítulos</button>
        </nav> : null}
        {download.isPending ? <p role="status">Añadiendo capítulos a la cola… Puedes seguir leyendo.</p> : null}
        {download.isSuccess ? <p role="status">{download.data} {download.data === 1 ? "capítulo añadido o ya disponible" : "capítulos añadidos o ya disponibles"}. Se conservan las copias existentes.</p> : null}
        {download.isError ? <p role="alert">No se pudo añadir toda la selección. Los capítulos ya añadidos se conservan; puedes reintentar sin duplicarlos.</p> : null}
        <Link className={styles.textLink} to="/downloads">Ver cola de descargas</Link>
      </> : null}
    </div> : null}
  </section>;
}
