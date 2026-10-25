import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, getChapterAvailability, getConnectorChapters, queueConnectorChapters } from "../api/client";
import type { LibraryReadingSource } from "../api/client";
import { prepareOnlineChapter, warmOnlineChapter } from "../reader/prepareOnlineChapter";
import { publicationStatus } from "../library/publicationStatus";
import styles from "./SeriesDetailPage.module.css";
import { ActionIcon } from "../app/ActionIcon";

export function OnlineChapters({seriesId, sources, source, onSourceChange, downloadOpen}: {
  seriesId: string;
  sources: LibraryReadingSource[];
  source: LibraryReadingSource;
  onSourceChange: (id: string) => void;
  downloadOpen: boolean;
}) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [descending, setDescending] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [statusFilter, setStatusFilter] = useState("all");
  const mounted = useRef(true);
  const warmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stopWarmup = () => { if (warmTimer.current !== null) clearTimeout(warmTimer.current); };
  useEffect(() => () => { if (warmTimer.current !== null) clearTimeout(warmTimer.current); }, []);
  const prepare = (remoteId: string | null) => {
    stopWarmup();
    if (remoteId === null || source.connectorId !== "yomira.metadata.mangadex") return;
    warmTimer.current = setTimeout(() => {
      void warmOnlineChapter(client, {seriesId, connectorId: source.connectorId,
        remoteSeriesId: source.remoteSeriesId, remoteId}).catch(() => {});
    }, 300);
  };
  const refreshRequested = useRef(false);
  useEffect(() => {
    if (downloadOpen) document.getElementById("series-download-selection")?.scrollIntoView?.({block: "nearest"});
  }, [downloadOpen]);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  const query = useQuery({
    queryKey: ["connector-chapters", source.connectorId, source.remoteSeriesId],
    queryFn: () => {
      const refresh = refreshRequested.current;
      refreshRequested.current = false;
      return getConnectorChapters(source.connectorId, source.remoteSeriesId, refresh);
    },
    staleTime: 60_000, retry: false,
  });
  const availability = useQuery({
    queryKey: ["chapter-availability", source.connectorId, source.remoteSeriesId],
    queryFn: () => getChapterAvailability(source.connectorId, source.remoteSeriesId),
    staleTime: 5_000, retry: false,
    refetchInterval: downloadOpen ? 5_000 : false,
  });
  const ready = new Set(availability.data?.readyRemoteIds ?? []);
  const busy = new Set(availability.data?.busyRemoteIds ?? []);
  const pendingIds = [...new Set((query.data?.data ?? []).flatMap((chapter) =>
    chapter.remoteId !== null && !ready.has(chapter.remoteId) && !busy.has(chapter.remoteId) ? [chapter.remoteId] : []))];
  const pendingSet = new Set(pendingIds);
  const selectedSet = new Set(selected);
  const selectedIds = pendingIds.filter((id) => selectedSet.has(id));
  const download = useMutation({
    mutationFn: (ids: string[]) => queueConnectorChapters(source.connectorId, source.remoteSeriesId, ids),
    retry: false,
    onSuccess: (_count, ids) => {
      const queued = new Set(ids);
      setSelected((current) => current.filter((id) => !queued.has(id)));
    },
    onSettled: async () => {
      await Promise.all([
        client.invalidateQueries({queryKey: ["chapter-availability", source.connectorId, source.remoteSeriesId]}),
        client.invalidateQueries({queryKey: ["download-jobs"]}),
        client.invalidateQueries({queryKey: ["library"]}),
        client.invalidateQueries({queryKey: ["reader-chapters", seriesId]}),
      ]);
    },
  });
  const read = useMutation({
    mutationFn: (remoteId: string) => prepareOnlineChapter(client, {seriesId, connectorId: source.connectorId, remoteSeriesId: source.remoteSeriesId, remoteId}),
    retry: false,
    onSuccess: (chapter) => {
      if (!mounted.current) return;
      if (chapter.seriesId !== seriesId) throw new Error("La fuente cambió la edición vinculada. Vuelve a abrir la ficha.");
      navigate(`/reader/${chapter.chapterId}?online=1`, {state: {originatingSeriesId: seriesId}});
    },
  });
  const chapters = (query.data?.data ?? []).filter((item) =>
    item.displayLabel.toLocaleLowerCase().includes(filter.trim().toLocaleLowerCase()) &&
    (statusFilter === "all" || (item.remoteId !== null && (statusFilter === "saved" ? ready.has(item.remoteId)
      : statusFilter === "busy" ? busy.has(item.remoteId) : !ready.has(item.remoteId) && !busy.has(item.remoteId)))));
  if (descending) chapters.reverse();
  const visible = chapters.slice(offset, offset + 100);
  const visibleIds = new Set(visible.flatMap((chapter) => chapter.remoteId === null ? [] : [chapter.remoteId]));
  const visiblePendingIds = [...visibleIds].filter((id) => pendingSet.has(id));
  const allVisibleSelected = visiblePendingIds.length > 0 && visiblePendingIds.every((id) => selectedSet.has(id));
  const hiddenSelectionCount = selectedIds.filter((id) => !visibleIds.has(id)).length;
  const error = query.error ?? read.error;
  return <section aria-labelledby="online-chapters-title" className={styles.chapters}>
    <header className={styles.chaptersHeader}><div>
      <p className={styles.eyebrow}>Disponible sin descargar</p>
      <h2 id="online-chapters-title">Leer online</h2>
    </div></header>
    <label className={styles.onlineControl}>Fuente de lectura
      <select value={source.seriesSourceId} disabled={read.isPending || download.isPending} onChange={(event) => {
        setOffset(0); setFilter(""); setSelected([]); setStatusFilter("all"); read.reset(); download.reset(); onSourceChange(event.target.value);
      }}>{sources.map((item) => <option key={item.seriesSourceId} value={item.seriesSourceId}>{item.displayName}</option>)}</select>
    </label>
    <p className={styles.statusMessage}>
      Ficha de {source.displayName}: {" "}
      {[source.author, source.genres?.join(" · "), source.status ? publicationStatus(source.status) : null].filter(Boolean).join(" · ")}
    </p>
    {query.isPending ? <p role="status">Consultando capítulos en {source.displayName}…</p> : null}
    {error ? <div role="alert" className={styles.errorState}>
      <p>{error.message}{error instanceof ApiError && error.traceId ? ` · Referencia: ${error.traceId}` : ""}</p>
      {query.isError ? <button type="button" className={styles.secondaryButton} onClick={() => void query.refetch()}>Reintentar capítulos</button> : null}
    </div> : null}
    {query.data ? <>
      <div className={styles.onlineTools}>
        {query.data.data.find((chapter) => chapter.remoteId !== null) ? <button type="button" className={styles.primaryButton} disabled={read.isPending}
          onClick={() => read.mutate(query.data.data.find((chapter) => chapter.remoteId !== null)!.remoteId!)}><ActionIcon name="play" />Empezar a leer online</button> : null}
      </div>
      <div className={styles.onlineTools}>
        <label className={styles.onlineControl}>Buscar capítulo
          <input value={filter} onChange={(event) => {setFilter(event.target.value); setOffset(0);}} placeholder="Número o título" />
        </label>
        <label className={styles.onlineControl}>Estado de capítulos<select value={statusFilter} disabled={!availability.isSuccess} onChange={(event) => {setStatusFilter(event.target.value); setOffset(0);}}>
          <option value="all">Todos</option><option value="saved">Guardados</option><option value="busy">Pendientes en cola</option><option value="online">Disponibles online</option>
        </select></label>
        <button type="button" className={styles.secondaryButton} onClick={() => {setDescending(!descending); setOffset(0);}}>Orden: {descending ? "descendente" : "ascendente"}</button>
        <button type="button" aria-label="Actualizar capítulos" className={styles.secondaryButton} disabled={query.isFetching} onClick={() => {setOffset(0); refreshRequested.current = true; void query.refetch();}}><ActionIcon name="refresh" />{query.isFetching ? "Actualizando…" : "Actualizar"}</button>
      </div>
      <p role="status" className={styles.statusMessage}>{chapters.length} capítulos disponibles. Las páginas se cargan al abrir el capítulo.</p>
      {downloadOpen ? <div id="series-download-selection" className={styles.selectionBar}>
        <p role="status" className={styles.selectionSummary}>{selectedIds.length} {selectedIds.length === 1 ? "seleccionado" : "seleccionados"}{hiddenSelectionCount > 0 ? ` · ${hiddenSelectionCount} fuera de esta página` : ""}</p>
        <button type="button" className={styles.secondaryButton} disabled={!availability.isSuccess || visiblePendingIds.length === 0 || download.isPending}
          onClick={() => setSelected((current) => allVisibleSelected ? current.filter((id) => !visibleIds.has(id))
            : [...new Set([...current, ...visiblePendingIds])])}>{allVisibleSelected ? "Desmarcar visibles" : "Seleccionar pendientes visibles"}</button>
        {selectedIds.length > 0 ? <>
          <button type="button" className={styles.primaryButton} disabled={!availability.isSuccess || download.isPending} onClick={() => download.mutate(selectedIds)}>Descargar selección ({selectedIds.length})</button>
          <button type="button" className={styles.secondaryButton} disabled={download.isPending} onClick={() => setSelected([])}>Limpiar selección</button>
        </> : null}
        <details className={styles.cbrSelectionPanel}><summary>Descargar todos los pendientes</summary>
          <p className={styles.statusMessage}>Incluye los pendientes de todas las páginas, aunque haya una búsqueda activa. Se conservan los capítulos guardados y los que ya están en cola.</p>
          <button type="button" className={styles.secondaryButton} disabled={!availability.isSuccess || pendingIds.length === 0 || download.isPending}
            onClick={() => download.mutate(pendingIds)}>Descargar pendientes del manga ({pendingIds.length})</button>
        </details>
        {availability.isPending ? <p role="status">Comprobando capítulos guardados y en cola…</p> : null}
        {availability.isError ? <p role="alert">No se pudo comprobar el estado. <button type="button" className={styles.secondaryButton} onClick={() => void availability.refetch()}>Reintentar estados</button></p> : null}
      </div> : null}
      {download.isPending ? <p role="status">Añadiendo a la cola de descargas…</p> : null}
      {download.isSuccess ? <p role="status">{download.data} capítulos añadidos o ya disponibles. Puedes continuar leyendo.</p> : null}
      {download.isError ? <p role="alert">No se pudo completar la selección. Los trabajos añadidos se conservan; puedes reintentar sin duplicarlos.</p> : null}
      <ul aria-label="Capítulos online" className={`${styles.chapterList} ${styles.onlineList}`}>
        {visible.map((chapter, index) => <li key={chapter.remoteId ?? index} className={styles.chapterItem}>
          <div className={styles.chapterSelect}>
            {downloadOpen ? <input aria-label={`Seleccionar ${chapter.displayLabel}`} type="checkbox" disabled={!availability.isSuccess || chapter.remoteId === null || ready.has(chapter.remoteId) || busy.has(chapter.remoteId) || download.isPending}
              checked={chapter.remoteId !== null && pendingSet.has(chapter.remoteId) && selectedSet.has(chapter.remoteId)} onChange={(event) => {
                const id = chapter.remoteId;
                if (id !== null) setSelected((current) => event.target.checked ? [...new Set([...current, id])] : current.filter((value) => value !== id));
              }} /> : null}
            <span>{chapter.displayLabel}{chapter.language ? ` · ${chapter.language}` : ""}
              <small className={styles.chapterState}> · {chapter.remoteId === null ? "Sin acceso de lectura" : ready.has(chapter.remoteId) ? "Guardado" : busy.has(chapter.remoteId) ? "En cola" : availability.isSuccess ? "Online" : "Estado sin comprobar"}</small></span>
          </div>
          <button type="button" className={styles.secondaryButton} disabled={chapter.remoteId === null || read.isPending}
            onPointerEnter={() => prepare(chapter.remoteId)} onPointerLeave={stopWarmup}
            onFocus={() => prepare(chapter.remoteId)} onBlur={stopWarmup}
            aria-label={`Leer online ${chapter.displayLabel}`} onClick={() => {if (chapter.remoteId) read.mutate(chapter.remoteId);}}>
            <ActionIcon name="play" />{read.isPending && read.variables === chapter.remoteId ? "Abriendo…" : "Leer online"}
          </button>
        </li>)}
      </ul>
      {chapters.length === 0 ? <p role="status">La fuente no devuelve capítulos con este filtro. Puedes actualizar la lista o elegir otra fuente vinculada.</p> : null}
      {chapters.length > 100 ? <nav aria-label="Paginación de capítulos online" className={styles.chapterPagination}>
        <button type="button" className={styles.secondaryButton} disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Anterior</button>
        <span>Página {Math.floor(offset / 100) + 1} de {Math.ceil(chapters.length / 100)}</span>
        <button type="button" className={styles.secondaryButton} disabled={offset + 100 >= chapters.length} onClick={() => setOffset(offset + 100)}>Siguiente</button>
      </nav> : null}
    </> : null}
  </section>;
}
