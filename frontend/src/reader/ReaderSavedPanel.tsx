import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { getRecentReaderProgress, type ReaderChapter } from "../api/client";
import { ActionIcon } from "../app/ActionIcon";
import { readReaderLibrary } from "./readerLibrary";
import { loadReaderLibrary, putReaderBookmark, deleteReaderBookmark, readerLibraryError, READER_LIBRARY_QUERY_KEY, type StoredReaderLibrary } from "../api/readerLibrary";
import styles from "./ReaderPage.module.css";

export function ReaderSavedPanel({ chapter, page, canBookmark, onPage, onClose }: {
  chapter: ReaderChapter; page: number; canBookmark: boolean; onPage: (page: number) => void; onClose: () => void;
}) {
  const [legacy] = useState(readReaderLibrary);
  const client = useQueryClient();
  const libraryQuery = useQuery({ queryKey: READER_LIBRARY_QUERY_KEY, queryFn: loadReaderLibrary,
    staleTime: 0, retry: false, refetchOnMount: "always", refetchOnWindowFocus: true });
  const library = libraryQuery.data ?? legacy;
  const [saving, setSaving] = useState(false);
  const [tab, setTab] = useState<"bookmarks" | "history">("bookmarks");
  const [saveError, setSaveError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { closeRef.current?.focus(); }, []);
  const history = useQuery({ queryKey: ["reader", "progress", "recent", 20, "chapters"],
    queryFn: () => getRecentReaderProgress(20), enabled: tab === "history", staleTime: 0, retry: false });
  const bookmarks = library.bookmarks.filter((item) => item.seriesId === chapter.seriesId)
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  const marked = bookmarks.some((item) => item.chapterId === chapter.chapterId && item.pageIndex === page - 1);
  async function persist(action: () => Promise<StoredReaderLibrary>) {
    setSaving(true); setSaveError(null);
    try {
      await client.cancelQueries({ queryKey: READER_LIBRARY_QUERY_KEY });
      client.setQueryData(READER_LIBRARY_QUERY_KEY, await action());
    } catch (error) { setSaveError(readerLibraryError(error)); }
    finally { setSaving(false); }
  }
  return <section id="reader-saved" className={`${styles.navigator} ${styles.savedPanel}`} aria-labelledby="reader-saved-title">
    <header className={styles.savedHeader}><h2 id="reader-saved-title">Tu lectura</h2>
      <button ref={closeRef} className={styles.secondaryButton} type="button" onClick={onClose} aria-label="Cerrar marcadores e historial"><ActionIcon name="close" /></button></header>
    <div className={styles.savedTabs} role="group" aria-label="Elegir lista de lectura">
      <button className={styles.secondaryButton} type="button" aria-pressed={tab === "bookmarks"} onClick={() => setTab("bookmarks")}>Marcadores</button>
      <button className={styles.secondaryButton} type="button" aria-pressed={tab === "history"} onClick={() => setTab("history")}>Historial</button>
    </div>
    {tab === "bookmarks" ? <>
      <button className={styles.secondaryButton} type="button" disabled={!canBookmark || marked || saving || !libraryQuery.isSuccess} onClick={() => void persist(() => putReaderBookmark({
        seriesId: chapter.seriesId, chapterId: chapter.chapterId, pageIndex: page - 1,
        chapterLabel: chapter.displayLabel, createdAt: new Date().toISOString(),
      }))}><ActionIcon name="bookmark" />{marked ? "Página marcada" : `Marcar página ${page}`}</button>
      <p className={styles.panelHint}>Marcadores de esta obra, guardados en la biblioteca de Yomira.</p>
      {libraryQuery.isPending ? <p role="status">Cargando y trasladando marcadores anteriores…</p> : null}
      {libraryQuery.isError ? <div role="alert"><p>{readerLibraryError(libraryQuery.error)}</p>
        <button className={styles.secondaryButton} type="button" onClick={() => void libraryQuery.refetch()}>Reintentar marcadores</button></div> : null}
      {libraryQuery.data?.migrationWarning ? <p role="status">{libraryQuery.data.migrationWarning}</p> : null}
      {saving ? <p role="status">Guardando en la biblioteca…</p> : null}
      {saveError ? <p role="alert">No se pudieron guardar los marcadores. {saveError} Los datos anteriores se conservan.</p> : null}
      {!bookmarks.length ? <p>No tienes marcadores en esta obra.</p> : <ul className={styles.savedList}>{bookmarks.map((item) => {
        const current = item.chapterId === chapter.chapterId;
        const unavailable = current && item.pageIndex >= chapter.pageCount;
        const label = `${item.chapterLabel} · Página ${item.pageIndex + 1}`;
        return <li key={`${item.chapterId}:${item.pageIndex}`}>
          {current ? <button className={styles.secondaryButton} type="button" disabled={unavailable}
            onClick={() => { onPage(item.pageIndex + 1); onClose(); }}>{label}{unavailable ? " · No disponible" : ""}</button>
            : <Link className={styles.secondaryButton} to={`/reader/${item.chapterId}`} state={{ originatingSeriesId: item.seriesId, readerBookmarkPage: item.pageIndex + 1 }} onClick={onClose}>{label}</Link>}
          <button className={styles.secondaryButton} type="button" disabled={saving || !libraryQuery.isSuccess} aria-label={`Quitar marcador: ${label}`}
            onClick={() => void persist(() => deleteReaderBookmark(item.chapterId, item.pageIndex))}><ActionIcon name="close" /></button>
        </li>;
      })}</ul>}
    </> : <>
      <p className={styles.panelHint}>Últimos 20 capítulos con progreso guardado, de todas tus obras.</p>
      {history.isPending ? <p role="status">Cargando historial…</p> : history.isError ? <div role="alert"><p>No se pudo cargar el historial.</p>
        <button className={styles.secondaryButton} type="button" onClick={() => void history.refetch()}>Reintentar historial</button></div>
        : !history.data?.data.length ? <p>No hay capítulos en el historial.</p>
        : <ul className={styles.savedList}>{history.data.data.map((item) => <li key={item.chapterId}>
          <Link className={styles.secondaryButton} to={`/reader/${item.chapterId}`} state={{ originatingSeriesId: item.seriesId }} onClick={onClose}>
            {item.seriesDisplayTitle} · {item.chapterDisplayLabel} · Página {item.pageIndex + 1}</Link>
        </li>)}</ul>}
    </>}
  </section>;
}
