import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  ApiError,
  getReaderChaptersPage,
  type ReaderChapter,
} from "../api/client";
import { CbrExportAction } from "../series/CbrExportAction";
import styles from "../series/SeriesDetailPage.module.css";

export function SeriesExportAction({ seriesId }: { seriesId: string }) {
  const client = useQueryClient();
  const [scope, setScope] = useState("all");
  const [first, setFirst] = useState(0);
  const [last, setLast] = useState<number | null>(null);
  const chaptersQuery = useQuery({
    queryKey: ["usb-series-chapters", seriesId],
    queryFn: async () => {
      const chapters: ReaderChapter[] = [];
      let total: number | null = null;
      do {
        const offset = chapters.length;
        const page = await client.fetchQuery({
          queryKey: ["reader-chapters", seriesId, offset],
          queryFn: () => getReaderChaptersPage({ seriesId, limit: 100, offset }),
          staleTime: 0,
          retry: false,
        });
        total ??= page.meta.total;
        if (page.meta.total !== total || (page.data.length === 0 && offset < total)) {
          throw new Error("La biblioteca cambió mientras se preparaba la exportación. Actualiza los capítulos.");
        }
        chapters.push(...page.data);
      } while (chapters.length < total);
      if (new Set(chapters.map((chapter) => chapter.chapterId)).size !== chapters.length) {
        throw new Error("La biblioteca cambió mientras se preparaba la exportación. Actualiza los capítulos.");
      }
      return chapters;
    },
    staleTime: 30_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const chapters = chaptersQuery.data ?? [];
  const end = last ?? chapters.length - 1;
  const selected = scope === "all" ? chapters : chapters.slice(first, end + 1);
  const pageCount = selected.reduce((count, chapter) => count + chapter.pageCount, 0);
  const error = chaptersQuery.error;

  return (
    <section aria-label="Exportar manga completo" className={styles.cbrExport}>
      <h3>Exportar manga a USB</h3>
      <p className={styles.statusMessage}>Un archivo por manga. En los lectores conectados se guarda en Books. Se incluyen todos los capítulos descargados, también los de otras páginas de la lista.</p>
      <fieldset className={styles.exportFields}>
        <label className={styles.cbrTargetField}>
          <span>Qué capítulos exportar</span>
          <select value={scope} onChange={(event) => { setScope(event.currentTarget.value); }}>
            <option value="all">Todos los descargados</option>
            <option value="range">Un intervalo</option>
          </select>
        </label>
        {scope === "range" && chapters.length > 0 ? <div className={styles.exportRange}>
          <label className={styles.cbrTargetField}>
            <span>Desde</span>
            <select value={first} onChange={(event) => {
              const index = Number(event.currentTarget.value);
              setFirst(index);
              if (index > end) setLast(index);
            }}>
              {chapters.map((chapter, index) => <option key={chapter.chapterId} value={index}>{chapter.displayLabel}</option>)}
            </select>
          </label>
          <label className={styles.cbrTargetField}>
            <span>Hasta</span>
            <select value={end} onChange={(event) => { setLast(Number(event.currentTarget.value)); }}>
              {chapters.map((chapter, index) => index >= first
                ? <option key={chapter.chapterId} value={index}>{chapter.displayLabel}</option> : null)}
            </select>
          </label>
        </div> : null}
        <button className={styles.secondaryButton} disabled={chaptersQuery.isFetching} type="button" onClick={() => {
          setFirst(0); setLast(null); void chaptersQuery.refetch();
        }}>Actualizar capítulos</button>
      </fieldset>
      {chaptersQuery.isPending ? <p role="status">Preparando todos los capítulos descargados…</p> : null}
      {chaptersQuery.isSuccess ? <p>{selected.length} {selected.length === 1 ? "capítulo" : "capítulos"} · {pageCount.toLocaleString("es")} {pageCount === 1 ? "página" : "páginas"}.</p> : null}
      {pageCount > 10_000 ? <p role="status">Este grupo supera las 10.000 páginas. No se enviará un manga parcial; elige un intervalo más pequeño.</p> : null}
      <p className={styles.statusMessage}>Reúne el grupo elegido en un solo CBR con las páginas en orden. Admite hasta 10.000 páginas y 8 GiB.</p>
      <CbrExportAction seriesId={seriesId} chapterIds={chaptersQuery.isError ? [] : selected.map((chapter) => chapter.chapterId)} pageCount={pageCount} />
      {error ? <div className={styles.cbrError} role="alert">
        <p>{error instanceof Error ? error.message : "No se pudo preparar la exportación."}</p>
        {error instanceof ApiError ? <p>Código: {error.code ?? "Sin código"} · Traza: {error.traceId ?? "No disponible"}</p> : null}
      </div> : null}
    </section>
  );
}
