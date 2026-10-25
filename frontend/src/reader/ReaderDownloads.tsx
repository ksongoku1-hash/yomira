import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { ActionIcon } from "../app/ActionIcon";
import { Link } from "react-router-dom";
import { getChapterAvailability, getConnectorChapters, getReaderDownloadSource, getReaderChaptersPage,
  queueConnectorChapters } from "../api/client";
import type { ReaderChapter } from "../api/client";
import type { OnlineChapterTarget } from "./ReaderPage";
import { compareConnectorChapters, currentConnectorChapter } from "../api/chapterOrder";
import styles from "./ReaderPage.module.css";

const PAGE_SIZE = 100;

export function ReaderDownloads({ chapterId, displayLabel, headingId, online, open, onOpenChange, navigation }: {
  chapterId: string; displayLabel?: string; open: boolean; onOpenChange: (open: boolean) => void;
  headingId?: string; online?: boolean;
  navigation?: { chapter: ReaderChapter; onRead: (chapter: ReaderChapter | OnlineChapterTarget) => void;
    isPending: boolean; isError: boolean };
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [offset, setOffset] = useState(0);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const sectionRef = useRef<HTMLElement>(null);
  const [filter, setFilter] = useState("");
  const browsing = navigation !== undefined;
  const panelId = browsing ? "reader-chapters" : "reader-downloads";
  useEffect(() => {
    if (!open || !browsing) return;
    const closeOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !sectionRef.current?.contains(event.target)) onOpenChange(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open, browsing, onOpenChange]);
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
  const localChapters = useQuery({
    queryKey: ["reader-chapters", navigation?.chapter.seriesId, offset, "asc"],
    queryFn: () => getReaderChaptersPage({ seriesId: navigation!.chapter.seriesId, limit: PAGE_SIZE, offset, order: "asc" }),
    enabled: open && browsing && source === null, staleTime: 30_000, retry: false,
  });
  const current = currentConnectorChapter(chaptersQuery.data?.data ?? [], source?.chapterRemoteId,
    navigation?.chapter.displayLabel ?? displayLabel);
  const currentId = current?.remoteId ?? source?.chapterRemoteId ?? "";
  const chapters = useMemo(() => {
    const catalog = chaptersQuery.data?.data ?? [];
    if (!browsing) return catalog;
    const search = filter.trim().toLocaleLowerCase();
    return catalog.filter((chapter) => (current === undefined || chapter.language === current.language) &&
      chapter.displayLabel.toLocaleLowerCase().includes(search)).sort(compareConnectorChapters);
  }, [chaptersQuery.data, browsing, current, filter]);
  const ids = [...new Set((chaptersQuery.data?.data ?? []).flatMap((chapter) => chapter.remoteId === null ? [] : [chapter.remoteId]))];
  const ready = new Set(availability.data?.readyRemoteIds ?? []);
  const busy = new Set(availability.data?.busyRemoteIds ?? []);
  const pendingIds = ids.filter((id) => !ready.has(id) && !busy.has(id));
  const pendingSet = new Set(pendingIds);
  const selectedSet = new Set(selected);
  const selectedIds = pendingIds.filter((id) => selectedSet.has(id));
  const visible = chapters.slice(offset, offset + PAGE_SIZE);
  const visibleIds = new Set(visible.flatMap((chapter) => chapter.remoteId === null ? [] : [chapter.remoteId]));
  const visiblePendingIds = [...visibleIds].filter((id) => pendingSet.has(id));
  const allVisibleSelected = visiblePendingIds.length > 0 && visiblePendingIds.every((id) => selectedSet.has(id));
  const hiddenSelectionCount = selectedIds.filter((id) => !visibleIds.has(id)).length;
  const download = useMutation({
    mutationFn: (chapterIds: string[]) => {
      if (source == null) throw new Error("La fuente no está disponible.");
      return queueConnectorChapters(source.connectorId, source.remoteSeriesId, chapterIds);
    },
    retry: false,
    onSuccess: (_count, chapterIds) => {
      const queued = new Set(chapterIds);
      setSelected((current) => current.filter((id) => !queued.has(id)));
    },
    onSettled: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["download-jobs"] }),
        client.invalidateQueries({ queryKey: ["chapter-availability", source?.connectorId, source?.remoteSeriesId] }),
        client.invalidateQueries({ queryKey: ["library"] }),
      ]);
    },
  });
  const currentReady = source != null && availability.data?.readyRemoteIds.includes(currentId);
  const currentBusy = source != null && availability.data?.busyRemoteIds.includes(currentId);
  const canDownload = chaptersQuery.isSuccess && availability.isSuccess && pendingIds.length > 0 && !download.isPending;

  const toggle = <button ref={toggleRef} className={`${styles.secondaryButton} ${styles.toolbarButton} ${headingId ? styles.chapterTrigger : ""}`} type="button" aria-expanded={open}
      aria-label={headingId ? `Lista de capítulos: ${displayLabel}` : browsing ? "Lista de capítulos" : "Descargar"} title={browsing ? "Lista de capítulos" : "Descargar capítulos"}
      aria-controls={panelId} onClick={() => {
        onOpenChange(!open);
      }}>{headingId ? <><span className={styles.chapterTriggerText}>{displayLabel}</span><ActionIcon name="next" /></>
        : <><ActionIcon name={browsing ? "chapters" : "download"} /><span className={styles.toolbarLabel}>{browsing ? "Capítulos" : "Descargar"}</span></>}</button>;

  return <section ref={sectionRef} className={styles.downloadSection} aria-label={browsing ? "Capítulos del lector" : "Descargas del lector"}
    onKeyDown={(event) => {
      if (event.key !== "Escape" || !open) return;
      event.preventDefault(); event.stopPropagation();
      onOpenChange(false); toggleRef.current?.focus();
    }}>
    {headingId ? <h1 id={headingId} aria-label={displayLabel}>{toggle}</h1> : toggle}
    {open ? <div id={panelId} className={`${styles.downloadPanel} ${browsing ? styles.chapterDropdown : ""}`}>
      <div className={styles.panelHeading}><div><p className={styles.panelEyebrow}>{browsing ? "Tu lectura" : "Tu biblioteca offline"}</p>
        <h2>{browsing ? "Capítulos" : "Descargar capítulos"}</h2></div>
        <button className={`${styles.secondaryButton} ${styles.iconButton}`} type="button"
          aria-label={browsing ? "Cerrar capítulos" : "Cerrar descargas"} title={browsing ? "Cerrar capítulos" : "Cerrar descargas"} onClick={() => {
            onOpenChange(false); toggleRef.current?.focus();
          }}><ActionIcon name="close" /></button>
      </div>
      {!browsing && online !== undefined ? <p className={styles.panelHint}>{online
        ? "Online · descarga opcional" : "Guardado · disponible sin conexión"}</p> : null}
      <p className={styles.panelDescription}>{browsing ? "Cambia de capítulo o guarda otros para leer después." : "Guarda el capítulo actual o elige otros para leer después."}</p>
      {navigation?.isPending ? <p role="status">Abriendo capítulo…</p> : null}
      {navigation?.isError ? <p role="alert">No se pudo abrir el capítulo. Tu lectura actual se conserva; puedes reintentar.</p> : null}
      {browsing && source != null ? <label className={styles.chapterSearch}>Buscar capítulo en el lector
        <input placeholder="Número o título" value={filter} onChange={(event) => { setFilter(event.target.value); setOffset(0); }} />
      </label> : null}
      {sourceQuery.isPending ? <p role="status">Buscando la fuente del capítulo…</p> : null}
      {sourceQuery.isError ? <p role="alert">No se pudo consultar la fuente.
        <button type="button" className={styles.secondaryButton} onClick={() => void sourceQuery.refetch()}>Reintentar fuente</button>
      </p> : null}
      {source === null ? <p role="status">Este capítulo no tiene una fuente de descarga disponible. Puedes seguir leyendo tu copia local.</p> : null}
      {source === null && navigation ? <>
        {localChapters.isPending ? <p role="status">Cargando capítulos guardados…</p> : null}
        {localChapters.isError ? <p role="alert">No se pudieron consultar los capítulos guardados.
          <button type="button" className={styles.secondaryButton} onClick={() => void localChapters.refetch()}>Reintentar capítulos guardados</button></p> : null}
        <ul className={styles.downloadChapterList} aria-label="Capítulos guardados">
          {localChapters.data?.data.map((chapter) => <li key={chapter.chapterId}>
            <span className={styles.downloadChapterName}>{chapter.displayLabel}<small>Guardado</small></span>
            <button type="button" className={styles.secondaryButton} disabled={chapter.chapterId === chapterId || navigation.isPending}
              aria-current={chapter.chapterId === chapterId ? "page" : undefined} aria-label={`Leer ${chapter.displayLabel}`}
              onClick={() => navigation.onRead(chapter)}>{chapter.chapterId === chapterId ? "Leyendo" : "Leer"}</button>
          </li>)}
        </ul>
        {localChapters.data?.meta.total === 0 ? <p role="status">No hay otros capítulos guardados disponibles.</p> : null}
        {(localChapters.data?.meta.total ?? 0) > PAGE_SIZE ? <nav className={styles.readerTools} aria-label="Paginación de capítulos guardados">
          <button type="button" className={styles.secondaryButton} disabled={offset === 0 || localChapters.isFetching} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Capítulos anteriores</button>
          <span>Página {Math.floor(offset / PAGE_SIZE) + 1}</span>
          <button type="button" className={styles.secondaryButton} disabled={localChapters.isFetching || offset + PAGE_SIZE >= localChapters.data!.meta.total} onClick={() => setOffset(offset + PAGE_SIZE)}>Más capítulos</button>
        </nav> : null}
      </> : null}
      {source != null ? <>
        {chaptersQuery.isPending ? <p role="status">Cargando capítulos…</p> : null}
        {chaptersQuery.isError ? <p role="alert">No se pudo cargar la lista de capítulos.
          <button type="button" className={styles.secondaryButton} onClick={() => void chaptersQuery.refetch()}>Reintentar capítulos</button>
        </p> : null}
        {availability.isError ? <p role="alert">No se pudo comprobar qué capítulos están guardados o en cola.
          <button type="button" className={styles.secondaryButton} onClick={() => void availability.refetch()}>Reintentar estados</button>
        </p> : null}
        <div className={styles.readerTools}>
          <p role="status" className={styles.selectionSummary}>{selectedIds.length} {selectedIds.length === 1 ? "seleccionado" : "seleccionados"}{hiddenSelectionCount > 0 ? ` · ${hiddenSelectionCount} fuera de esta página` : ""}</p>
          <button type="button" className={styles.secondaryButton} disabled={!canDownload || visiblePendingIds.length === 0}
            onClick={() => setSelected((current) => allVisibleSelected ? current.filter((id) => !visibleIds.has(id))
              : [...new Set([...current, ...visiblePendingIds])])}>{allVisibleSelected ? "Desmarcar visibles" : "Seleccionar pendientes visibles"}</button>
          {selectedIds.length > 0 ? <button type="button" className={styles.secondaryButton} disabled={download.isPending}
            onClick={() => setSelected([])}>Limpiar selección</button> : null}
          <button type="button" className={styles.primaryButton}
            disabled={!canDownload || !ids.includes(currentId) || !!currentReady || !!currentBusy}
            onClick={() => download.mutate([currentId])}>
            <ActionIcon name="download" />{currentReady ? "Capítulo descargado" : currentBusy ? "Capítulo en cola" : "Descargar capítulo actual"}
          </button>
          {selectedIds.length > 0 ? <button type="button" className={styles.secondaryButton}
            disabled={!canDownload || selectedIds.length === 0}
            onClick={() => download.mutate(selectedIds)}>Descargar selección ({selectedIds.length})</button> : null}
          <details className={styles.downloadMore}><summary>Más opciones</summary>
            <p className={styles.panelDescription}>La descarga de pendientes incluye todas las páginas, aunque haya una búsqueda activa. Se conservan los capítulos guardados y los que ya están en cola.</p>
            <button type="button" className={styles.secondaryButton} disabled={!canDownload}
              onClick={() => download.mutate(pendingIds)}>Descargar pendientes del manga ({pendingIds.length})</button>
          </details>
        </div>
        <ul className={styles.downloadChapterList}>
          {visible.map((chapter, index) => <li key={`${chapter.remoteId}:${offset + index}`}
            data-current={chapter.remoteId === currentId ? "true" : undefined}>
            <label><input aria-label={chapter.displayLabel} type="checkbox" disabled={!availability.isSuccess || chapter.remoteId === null || ready.has(chapter.remoteId) || busy.has(chapter.remoteId) || download.isPending}
              checked={chapter.remoteId !== null && pendingSet.has(chapter.remoteId) && selectedSet.has(chapter.remoteId)}
              onChange={(event) => {
                const id = chapter.remoteId;
                if (id !== null) setSelected((current) => event.target.checked
                  ? [...new Set([...current, id])] : current.filter((value) => value !== id));
               }} /><span className={styles.downloadChapterName}>{chapter.displayLabel}
                 <small>{chapter.remoteId === null ? "Sin acceso de lectura" : ready.has(chapter.remoteId) ? "Guardado" : busy.has(chapter.remoteId) ? "En cola" : chapter.remoteId === currentId ? "Leyendo ahora" : "Disponible online"}</small>
               </span></label>
            {navigation ? <button type="button" className={styles.secondaryButton} aria-label={`Leer ${chapter.displayLabel}`}
              aria-current={chapter.remoteId === currentId ? "page" : undefined}
              disabled={chapter.remoteId === null || chapter.remoteId === currentId || navigation.isPending}
              onClick={() => { if (chapter.remoteId !== null) navigation.onRead({
                seriesId: navigation.chapter.seriesId, connectorId: source.connectorId,
                remoteSeriesId: source.remoteSeriesId, remoteId: chapter.remoteId, displayLabel: chapter.displayLabel,
              }); }}><ActionIcon name="play" />{chapter.remoteId === currentId ? "Leyendo" : "Leer"}</button> : null}
            <button type="button" className={`${styles.secondaryButton} ${styles.iconButton}`}
              aria-label={`Descargar ${chapter.displayLabel}`} title={`Descargar ${chapter.displayLabel}`}
              disabled={!canDownload || chapter.remoteId === null || ready.has(chapter.remoteId) || busy.has(chapter.remoteId)}
              onClick={() => { if (chapter.remoteId !== null) download.mutate([chapter.remoteId]); }}>
              <ActionIcon name="download" />
            </button>
          </li>)}
        </ul>
        {browsing && chaptersQuery.isSuccess && chapters.length === 0 ? <p role="status">No hay capítulos con esa búsqueda.</p> : null}
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
