import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import {
  ApiError,
  createConnectorDownloadJobs,
  getConnectorChapters,
  getChapterAvailability,
  getConnectorCover,
  getConnectors,
  getConnectorSearch,
  importConnectorLibrarySeries,
  type DownloadPriority,
  type ConnectorDescriptor,
  type ConnectorSearchResponse,
  type ConnectorSearchResult,
} from "../api/client";
import styles from "./SearchPage.module.css";

const MAX_SEARCH_RESULTS_PER_SOURCE = 100;

function SearchError({
  error,
  onRetry,
  retryLabel,
}: {
  error: unknown;
  onRetry: () => void;
  retryLabel: string;
}) {
  const apiError = error instanceof ApiError ? error : null;

  return (
    <div className={styles.errorState} role="alert">
      <div>
        <h2>No se pudo completar la solicitud</h2>
        <p>{apiError?.message ?? "Falló una operación de la interfaz."}</p>
        <p className={styles.errorDetails}>
          Código: <code>{apiError?.code ?? "Sin código de servidor"}</code>
          {apiError?.status !== null && apiError?.status !== undefined ? (
            <span> · HTTP {apiError.status}</span>
          ) : null}
        </p>
        {apiError?.traceId ? (
          <p className={styles.errorDetails}>
            Traza: <code>{apiError.traceId}</code>
          </p>
        ) : null}
      </div>
      <button className={styles.secondaryButton} onClick={onRetry} type="button">
        {retryLabel}
      </button>
    </div>
  );
}

function supportsSearch(connector: ConnectorDescriptor): boolean {
  return connector.capabilities.includes("SEARCH");
}

function connectorLabel(connectorId: string): string {
  const normalizedId = connectorId.toLowerCase();
  if (normalizedId === "yomira.mock") return "yomira.mock · prueba local";
  if (normalizedId === "yomira.fmd2.webtoons") return "Free Manga";
  if (normalizedId === "yomira.metadata.mangadex") return "MangaDex";
  return connectorId;
}

function resultSourceLabel(result: ConnectorSearchResult, connectorId: string): string {
  return result.sourceLabel?.trim() || connectorLabel(connectorId);
}

interface SearchResultEntry {
  result: ConnectorSearchResult;
  connector: ConnectorDescriptor;
  key: string;
}

interface GroupedSearchResult {
  key: string;
  title: string;
  coverUrl: string | null;
  coverLookup: { connectorId: string; remoteId: string } | null;
  sources: SearchResultEntry[];
}

function normalizedTitle(title: string): string {
  return title
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLocaleLowerCase("es")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

function chapterLabelKey(label: string): string {
  const normalized = label.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
  const match = /^(?:(?:capitulo|cap|chapter|ch|episodio|episode)\s*[.:#-]?\s*)?(\d+(?:[.,]\d+)?)$/.exec(normalized);
  if (!match) return normalized;
  const [integer, decimal = ""] = match[1]!.split(/[.,]/);
  const fraction = decimal.replace(/0+$/, "");
  return `number:${integer!.replace(/^0+(?=\d)/, "")}${fraction ? `.${fraction}` : ""}`;
}

function groupSearchResults(entries: SearchResultEntry[]): GroupedSearchResult[] {
  const groups = new Map<string, GroupedSearchResult>();

  for (const entry of entries) {
    const remoteTitle = entry.result.remoteTitle?.trim() || "";
    const title = remoteTitle || entry.result.remoteId ||
      entry.result.remoteUrl || "Serie sin título";
    const titleKey = remoteTitle ? normalizedTitle(remoteTitle) : "";
    const groupKey = titleKey
      ? `title:${titleKey}`
      : `entry:${entry.key}`;
    let group = groups.get(groupKey);
    if (group === undefined) {
      group = { key: groupKey, title, coverUrl: null, coverLookup: null, sources: [] };
      groups.set(groupKey, group);
    }
    group.sources.push(entry);
    if (group.coverUrl === null && entry.result.coverUrl !== null) {
      group.coverUrl = entry.result.coverUrl;
    }
    if (
      group.coverLookup === null &&
      entry.result.coverUrl === null &&
      entry.result.remoteId !== null &&
      entry.connector.capabilities.includes("METADATA")
    ) {
      group.coverLookup = {
        connectorId: entry.connector.connectorId,
        remoteId: entry.result.remoteId,
      };
    }
  }

  return [...groups.values()];
}

function SearchResultSource({
  result,
  connector,
  displayTitle,
  expanded,
  onToggle,
}: {
  result: ConnectorSearchResult;
  connector: ConnectorDescriptor;
  displayTitle: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const connectorId = connector.connectorId;
  const sourceLabel = resultSourceLabel(result, connectorId);
  const canListChapters = connector.capabilities.includes("CHAPTER_LIST");
  const canDownloadChapters = canListChapters && connector.capabilities.includes("PAGE_LIST");
  const canImportSeries = connector.capabilities.includes("METADATA") && result.remoteId !== null;
  const title = displayTitle;
  const queryClient = useQueryClient();
  const [rangeStartId, setRangeStartId] = useState("");
  const [rangeEndId, setRangeEndId] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [chapterFilter, setChapterFilter] = useState("");
  const [chapterOrder, setChapterOrder] = useState<"asc" | "desc">("asc");
  const [stateFilter, setStateFilter] = useState("all");
  const [languageFilter, setLanguageFilter] = useState("all");
  const [priority, setPriority] = useState<DownloadPriority>("NORMAL");
  const availability = useQuery({
    queryKey:["chapter-availability", connectorId, result.remoteId],
    queryFn:() => getChapterAvailability(connectorId, result.remoteId ?? ""),
    enabled:expanded && canDownloadChapters && result.remoteId !== null,
    staleTime:0, refetchOnMount:"always",
    refetchInterval:5_000, retry:false,
  });
  const chaptersQuery = useQuery({
    queryKey: ["connector-chapters", connectorId, result.remoteId],
    queryFn: () => getConnectorChapters(connectorId, result.remoteId ?? ""),
    enabled: expanded && canListChapters && result.remoteId !== null,
  });
  const importSeriesMutation = useMutation({
    mutationKey: ["connector-library-series", connectorId, result.remoteId],
    mutationFn: () => importConnectorLibrarySeries(connectorId, result.remoteId ?? ""),
    retry: false,
    onSuccess: async ({ data }) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["library"] }),
        queryClient.invalidateQueries({ queryKey: ["series", data.seriesId] }),
        queryClient.invalidateQueries({ queryKey: ["chapter-availability", connectorId, result.remoteId] }),
      ]);
    },
  });
  const createJobsMutation = useMutation({
    mutationKey: ["connector-download-jobs", connectorId, result.remoteId],
    mutationFn: ({ chapterRemoteIds, selectedPriority }: {
      chapterRemoteIds: string[];
      selectedPriority: DownloadPriority;
    }) => createConnectorDownloadJobs(connectorId, result.remoteId ?? "", {
      chapterRemoteIds,
      priority: selectedPriority,
    }),
    onSuccess: async () => {
      setSelectedIds([]);
      setRangeStartId("");
      setRangeEndId("");
      await queryClient.invalidateQueries({ queryKey: ["download-jobs"] });
      await queryClient.invalidateQueries({ queryKey: ["chapter-availability", connectorId, result.remoteId] });
    },
  });

  const chapters = [...(chaptersQuery.data?.data ?? [])].sort((left, right) => {
    for (let index=0; index < Math.max(left.chapterSortKey.length, right.chapterSortKey.length); index++) {
      const difference = (left.chapterSortKey[index] ?? -1) - (right.chapterSortKey[index] ?? -1);
      if (difference) return difference;
    }
    return left.displayLabel.localeCompare(right.displayLabel, "es", {numeric:true});
  });
  const labelCounts = new Map<string, number>();
  for (const chapter of chapters) {
    const key = chapterLabelKey(chapter.displayLabel);
    labelCounts.set(key, (labelCounts.get(key) ?? 0) + 1);
  }
  const localLabels = new Set(availability.data?.readyLabels.map(chapterLabelKey) ?? []);
  const readyIds = new Set(availability.data?.readyRemoteIds ?? []);
  const busyIds = new Set(availability.data?.busyRemoteIds ?? []);
  function chapterState(chapter: typeof chapters[number]): "ready" | "busy" | "missing" {
    const key = chapterLabelKey(chapter.displayLabel);
    if ((chapter.remoteId && readyIds.has(chapter.remoteId)) ||
      (availability.data?.linked && labelCounts.get(key) === 1 && localLabels.has(key))) return "ready";
    if (chapter.remoteId && busyIds.has(chapter.remoteId)) return "busy";
    return "missing";
  }
  const missingChapters = chapters.filter((chapter) => chapter.remoteId !== null && chapterState(chapter) === "missing");
  const selectableRemoteIds = availability.isSuccess ? [...new Set(
    missingChapters.flatMap((chapter) => chapter.remoteId === null ? [] : [chapter.remoteId]),
  )] : [];
  const chapterLabels = new Map<string, string>();
  for (const chapter of chapters) {
    if (chapter.remoteId !== null) chapterLabels.set(chapter.remoteId, chapter.displayLabel);
  }
  const rangeStartIndex = selectableRemoteIds.indexOf(rangeStartId);
  const createdJobCount = createJobsMutation.data?.data.filter((job) => job.created).length ?? 0;
  const reusedJobCount = createJobsMutation.data?.data.filter((job) => !job.created).length ?? 0;
  const selectedRemoteIds = selectableRemoteIds.filter((id) => selectedIds.includes(id));
  const languages = [...new Set(chapters.map((chapter) => chapter.language ?? "unknown"))].sort();
  const filteredChapters = chapters.filter((chapter) =>
    normalizedTitle(chapter.displayLabel).includes(normalizedTitle(chapterFilter)) &&
    (stateFilter === "all" || chapterState(chapter) === stateFilter) &&
    (languageFilter === "all" || (chapter.language ?? "unknown") === languageFilter),
  );
  const visibleChapters = chapterOrder === "desc" ? [...filteredChapters].reverse() : filteredChapters;
  const visibleRemoteIds = visibleChapters.flatMap((chapter) =>
    chapter.remoteId === null || !selectableRemoteIds.includes(chapter.remoteId) ? [] : [chapter.remoteId],
  );

  function changeRangeStart(remoteId: string) {
    createJobsMutation.reset();
    const nextStartIndex = selectableRemoteIds.indexOf(remoteId);
    const currentEndIndex = selectableRemoteIds.indexOf(rangeEndId);
    setRangeStartId(remoteId);
    const nextEndIndex = currentEndIndex < nextStartIndex || currentEndIndex - nextStartIndex >= 100
      ? nextStartIndex : currentEndIndex;
    setRangeEndId(selectableRemoteIds[nextEndIndex] ?? "");
    setSelectedIds(nextStartIndex < 0 ? [] : selectableRemoteIds.slice(nextStartIndex, nextEndIndex + 1));
  }

  function toggleChapter(remoteId: string) {
    createJobsMutation.reset();
    setRangeStartId("");
    setRangeEndId("");
    setSelectedIds((current) => {
      const available = current.filter((id) => selectableRemoteIds.includes(id));
      return available.includes(remoteId)
        ? available.filter((id) => id !== remoteId)
        : available.length < 100 ? [...available, remoteId] : available;
    });
  }

  function submitSelectedChapters() {
    if (!canDownloadChapters || selectedRemoteIds.length === 0 || result.remoteId === null) return;
    createJobsMutation.mutate({
      chapterRemoteIds: selectedRemoteIds,
      selectedPriority: priority,
    });
  }

  return (
    <div className={styles.sourceResult}>
      <div className={styles.sourceHeader}>
        {canListChapters || canImportSeries ? (
          <button
            aria-label={canListChapters
              ? `${expanded ? "Ocultar" : "Ver"} capítulos de ${title} en ${sourceLabel}`
              : `${expanded ? "Ocultar" : "Ver"} ficha de ${title} en ${sourceLabel}`}
            aria-expanded={expanded}
            className={styles.sourceToggle}
            disabled={canListChapters && result.remoteId === null}
            onClick={onToggle}
            type="button"
          >
            <span>{sourceLabel}</span>
          </button>
        ) : (
          <span className={styles.sourceBadge}>{sourceLabel}</span>
        )}
      </div>
      {expanded ? (
        <div className={styles.sourceDetail}>
          <div className={styles.sourceDetailHeader}>
            <p className={styles.sourceAvailability}>
              {canDownloadChapters
                ? "Descarga disponible"
                : canListChapters
                  ? "Capítulos consultables; descarga no disponible"
                  : "Fuente de metadatos; descarga no disponible desde esta fuente"}
            </p>
            {canImportSeries ? (
              <button
                className={styles.secondaryButton}
                disabled={importSeriesMutation.isPending || importSeriesMutation.isSuccess}
                onClick={() => importSeriesMutation.mutate()}
                type="button"
              >
                {importSeriesMutation.isPending
                  ? "Añadiendo…"
                  : importSeriesMutation.isSuccess
                    ? "En biblioteca"
                    : "Añadir a biblioteca"}
              </button>
            ) : null}
          </div>
          {importSeriesMutation.isError ? (
            <SearchError
              error={importSeriesMutation.error}
              onRetry={() => importSeriesMutation.mutate()}
              retryLabel="Reintentar alta en biblioteca"
            />
          ) : null}
          {importSeriesMutation.data ? (
            <p className={styles.successMessage} role="status">
              {importSeriesMutation.data.data.created
                ? `Se añadió «${importSeriesMutation.data.data.seriesTitle}» a la biblioteca.`
                : `«${importSeriesMutation.data.data.seriesTitle}» ya está en la biblioteca.`}
            </p>
          ) : null}
          {canListChapters ? (
            <div aria-label={`Capítulos de ${title}`} className={styles.chapterPanel}>
              {chaptersQuery.isPending ? (
                <p aria-live="polite" className={styles.statusMessage} role="status">
                  Cargando capítulos…
                </p>
              ) : chaptersQuery.isError ? (
                <SearchError
                  error={chaptersQuery.error}
                  onRetry={() => void chaptersQuery.refetch()}
                  retryLabel="Reintentar capítulos"
                />
              ) : chaptersQuery.data?.data.length === 0 ? (
                <p className={styles.statusMessage} role="status">
                  Esta serie no tiene capítulos disponibles.
                </p>
              ) : (
                <>
                  <div className={styles.bulkActions}>
                    <button className={`${styles.secondaryButton} ${styles.orderButton}`} type="button"
                      aria-label="Cambiar orden de capítulos para descargar"
                      aria-pressed={chapterOrder === "desc"}
                      title={chapterOrder === "asc" ? "Ascendente: primero los capítulos iniciales" : "Descendente: primero los capítulos finales"}
                      onClick={() => setChapterOrder((current) => current === "asc" ? "desc" : "asc")}>
                      <span aria-hidden="true">{chapterOrder === "asc" ? "↑" : "↓"}</span>
                    </button>
                    <span className={styles.chapterMetadata} role="status">
                      {visibleChapters.length} de {chapters.length} capítulos visibles
                    </span>
                  </div>
                  {canDownloadChapters ? (
                    <>
                      <div className={styles.chapterSummary}>
                        <strong>{title}</strong>
                        <span>{sourceLabel} · {chapters.length} capítulos disponibles</span>
                      </div>
                      <div className={styles.selectionTools}>
                        <label className={styles.rangeField}>
                          Desde
                          <select
                            aria-label="Desde"
                            disabled={createJobsMutation.isPending || selectableRemoteIds.length === 0}
                            onChange={(event) => changeRangeStart(event.currentTarget.value)}
                            value={rangeStartId}
                          >
                            <option value="">Elige capítulo inicial</option>
                            {selectableRemoteIds.map((remoteId) => (
                              <option key={remoteId} value={remoteId}>
                                {chapterLabels.get(remoteId) ?? remoteId}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className={styles.rangeField}>
                          Hasta
                          <select
                            aria-label="Hasta"
                            disabled={createJobsMutation.isPending || rangeStartIndex < 0}
                            onChange={(event) => {
                              createJobsMutation.reset();
                              const remoteId = event.currentTarget.value;
                              const endIndex = selectableRemoteIds.indexOf(remoteId);
                              setRangeEndId(remoteId);
                              setSelectedIds(rangeStartIndex >= 0 && endIndex >= rangeStartIndex &&
                                endIndex - rangeStartIndex < 100
                                ? selectableRemoteIds.slice(rangeStartIndex, endIndex + 1) : []);
                            }}
                            value={rangeEndId}
                          >
                            <option value="">Elige capítulo final</option>
                            {selectableRemoteIds.map((remoteId, index) => (
                              <option
                                disabled={index < rangeStartIndex || index - rangeStartIndex >= 100}
                                key={remoteId}
                                value={remoteId}
                              >
                                {chapterLabels.get(remoteId) ?? remoteId}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className={styles.priorityField}>
                          Prioridad
                          <select
                            disabled={createJobsMutation.isPending}
                            onChange={(event) => setPriority(event.currentTarget.value as DownloadPriority)}
                            value={priority}
                          >
                            <option value="HIGH">Alta</option>
                            <option value="NORMAL">Normal</option>
                            <option value="LOW">Baja</option>
                          </select>
                        </label>
                      </div>
                      <div className={styles.chapterFilters}>
                      <label className={styles.rangeField}>
                        Filtrar capítulos
                        <input type="search" value={chapterFilter}
                          onChange={(event) => setChapterFilter(event.currentTarget.value)}
                          placeholder="Número o título" />
                      </label>
                      <label className={styles.rangeField}>
                        Estado del capítulo
                        <select value={stateFilter} onChange={(event) => setStateFilter(event.currentTarget.value)}>
                          <option value="all">Todos los estados</option>
                          <option value="missing">Sin descargar</option>
                          <option value="ready">Ya descargados</option>
                          <option value="busy">En cola o en curso</option>
                        </select>
                      </label>
                      <label className={styles.rangeField}>
                        Idioma del capítulo
                        <select value={languageFilter} onChange={(event) => setLanguageFilter(event.currentTarget.value)}>
                          <option value="all">Todos los idiomas</option>
                          {languages.map((language) => <option value={language} key={language}>
                            {language === "unknown" ? "Sin idioma indicado" : language}
                          </option>)}
                        </select>
                      </label>
                      </div>
                      <div className={styles.bulkActions}>
                        <button className={styles.secondaryButton} type="button"
                          disabled={availability.isFetching || createJobsMutation.isPending}
                          onClick={() => {void availability.refetch();}}>
                          {availability.isFetching ? "Comprobando capítulos…" : "Comprobar descargados y faltantes"}
                        </button>
                        <button className={styles.secondaryButton} type="button"
                          disabled={!availability.isSuccess || createJobsMutation.isPending || visibleRemoteIds.length === 0}
                          onClick={() => {
                            createJobsMutation.reset(); setRangeStartId(""); setRangeEndId("");
                            setSelectedIds(visibleRemoteIds.slice(0, 100));
                          }}>Seleccionar pendientes visibles{visibleRemoteIds.length > 100 ? " (primeros 100)" : ""}</button>
                        <button className={styles.secondaryButton} type="button"
                          disabled={rangeStartIndex < 0 || createJobsMutation.isPending}
                          onClick={() => {
                            const range = selectableRemoteIds.slice(rangeStartIndex, rangeStartIndex+100);
                            setRangeEndId(range.at(-1) ?? ""); setSelectedIds(range); createJobsMutation.reset();
                          }}>Desde este capítulo (hasta 100)</button>
                        <button className={styles.secondaryButton} type="button"
                          disabled={createJobsMutation.isPending || visibleRemoteIds.length === 0}
                          onClick={() => {
                            createJobsMutation.reset();
                            setRangeStartId("");
                            setRangeEndId("");
                            setSelectedIds(visibleRemoteIds.slice(0, 100));
                          }}>
                          {visibleRemoteIds.length > 100 ? "Seleccionar primeros 100" : "Seleccionar visibles"}
                        </button>
                        <button className={styles.secondaryButton} type="button"
                          disabled={createJobsMutation.isPending || visibleRemoteIds.length === 0}
                          onClick={() => {
                            createJobsMutation.reset(); setRangeStartId(""); setRangeEndId("");
                            const hidden = selectedRemoteIds.filter((id) => !visibleRemoteIds.includes(id));
                            const inverted = visibleRemoteIds.filter((id) => !selectedRemoteIds.includes(id));
                            setSelectedIds([...hidden, ...inverted].slice(0, 100));
                          }}>Invertir selección visible</button>
                        <button className={styles.secondaryButton} type="button"
                          disabled={createJobsMutation.isPending || selectedRemoteIds.length === 0}
                          onClick={() => {
                            createJobsMutation.reset();
                            setSelectedIds([]);
                            setRangeStartId("");
                            setRangeEndId("");
                          }}>Limpiar selección</button>
                      </div>
                      {availability.data && !availability.isError ? <p className={styles.chapterMetadata} role="status">
                        {chapters.filter((chapter) => chapterState(chapter) === "ready").length} disponibles para leer · {chapters.filter((chapter) => chapterState(chapter) === "busy").length} en cola o en curso · {missingChapters.length} sin descargar.
                        {availability.data.linked ? " Comparación con esta fuente y la edición vinculada. Los números repetidos se comprueban por su referencia de origen."
                          : " Esta fuente aún no está vinculada a una edición de tu biblioteca; solo se muestran sus descargas registradas."}
                        {" El listado de la fuente puede estar incompleto."}
                      </p> : availability.isError ? <p role="status">No se pudo comparar con la biblioteca. Pulsa «Comprobar descargados y faltantes» para reintentar antes de descargar.</p> : <p role="status">Comprobando los capítulos que ya tienes…</p>}
                      <ul aria-label={`Lista de capítulos de ${title}`} className={styles.selectableChapters}>
                        {visibleChapters.map((chapter, index) => (
                          <li key={chapter.remoteId ?? `unavailable-${index}`}>
                            <label className={styles.chapterChoice}>
                              <input type="checkbox"
                                checked={chapter.remoteId !== null && selectedRemoteIds.includes(chapter.remoteId)}
                                disabled={createJobsMutation.isPending || chapter.remoteId === null ||
                                  !selectableRemoteIds.includes(chapter.remoteId) ||
                                  (selectedRemoteIds.length >= 100 && !selectedRemoteIds.includes(chapter.remoteId))}
                                onChange={() => { if (chapter.remoteId !== null) toggleChapter(chapter.remoteId); }} />
                              <span>{chapter.displayLabel}</span>
                              {availability.data ? <small aria-hidden="true">{chapterState(chapter) === "ready" ? "Ya descargado"
                                : chapterState(chapter) === "busy" ? "En cola o en curso" : "Sin descargar"}</small> : null}
                              {chapter.language ? <small>{chapter.language}</small> : null}
                            </label>
                          </li>
                        ))}
                      </ul>
                      {visibleChapters.length === 0 ? <p role="status" className={styles.chapterMetadata}>
                        No hay capítulos con estos filtros. Cambia el estado, idioma o búsqueda.
                      </p> : null}
                      {selectedRemoteIds.some((id) => !visibleRemoteIds.includes(id)) ? <p role="status" className={styles.chapterMetadata}>
                        Hay capítulos seleccionados fuera de los filtros actuales; también se descargarán.
                      </p> : null}
                      {visibleChapters.length === 0 ? <p role="status">No hay capítulos que coincidan.</p> : null}
                      <p className={styles.chapterMetadata} aria-live="polite">
                        {selectedRemoteIds.length} {selectedRemoteIds.length === 1
                          ? "capítulo seleccionado"
                          : "capítulos seleccionados"}. Marca capítulos sueltos o elige un rango en el orden
                        numérico de capítulos. Hasta 100 por selección. Los trabajos se procesan en segundo plano;
                        consulta «Cola de descargas» para ver su estado.
                      </p>
                      <button
                        className={styles.primaryButton}
                        disabled={selectedRemoteIds.length === 0 || createJobsMutation.isPending}
                        onClick={submitSelectedChapters}
                        type="button"
                      >
                        {createJobsMutation.isPending
                          ? "Añadiendo a la cola…"
                          : `Descargar (${selectedRemoteIds.length})`}
                      </button>
                      {createJobsMutation.isError ? (
                        <SearchError
                          error={createJobsMutation.error}
                          onRetry={submitSelectedChapters}
                          retryLabel="Reintentar creación de trabajos"
                        />
                      ) : null}
                      {createJobsMutation.data ? (
                        <p className={styles.successMessage} role="status">
                          {createdJobCount > 0 ? (createdJobCount === 1
                            ? "Se añadió 1 capítulo a la cola."
                            : `Se añadieron ${createdJobCount} capítulos a la cola.`) : ""}
                          {reusedJobCount > 0
                            ? ` ${reusedJobCount} capítulos ya descargados o en cola se conservaron sin duplicarlos.` : ""}
                          {createdJobCount > 0
                            ? " La descarga comenzará automáticamente; consulta «Cola de descargas» para ver su estado." : ""}
                        </p>
                      ) : null}
                    </>
                  ) : (
                    <>
                      <ol aria-label={`Lista de capítulos de ${title}`} className={styles.chapterList}>
                        {visibleChapters.map((chapter, index) => (
                          <li className={styles.chapterItem} key={`${chapter.remoteId ?? "chapter"}-${index}`}>
                            <span>{chapter.displayLabel}</span>
                            <span className={styles.chapterMetadata}>
                              {chapter.language ? `Idioma: ${chapter.language}` : "Idioma no indicado"}
                            </span>
                          </li>
                        ))}
                      </ol>
                      <p className={styles.chapterMetadata}>
                        Esta fuente permite consultar capítulos. La descarga no está habilitada.
                      </p>
                    </>
                  )}
                </>
              )}
            </div>
          ) : (
            <p className={styles.chapterMetadata}>
              Esta fuente solo aporta información de la serie; no tiene capítulos descargables.
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}

export function SearchResultCard({
  group,
  expandedKey,
  onToggleSource,
}: {
  group: GroupedSearchResult;
  expandedKey: string | null;
  onToggleSource: (sourceKey: string) => void;
}) {
  const [coverFailed, setCoverFailed] = useState(false);
  const coverFrameRef = useRef<HTMLDivElement | null>(null);
  const [coverVisible, setCoverVisible] = useState(false);
  const coverTitle = `Portada de ${group.title}`;
  const coverConnectorId = group.coverLookup?.connectorId;
  const coverRemoteId = group.coverLookup?.remoteId;
  useEffect(() => {
    if (group.coverUrl !== null || coverConnectorId === undefined || coverRemoteId === undefined) {
      return;
    }
    const element = coverFrameRef.current;
    if (element === null) return;
    if (typeof IntersectionObserver === "undefined") {
      setCoverVisible(true);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setCoverVisible(true);
        observer.disconnect();
      }
    }, { rootMargin: "160px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, [group.coverUrl, coverConnectorId, coverRemoteId]);
  const coverQuery = useQuery({
    queryKey: ["connector-cover", coverConnectorId, coverRemoteId, group.title],
    queryFn: () => getConnectorCover(
      coverConnectorId ?? "", coverRemoteId ?? "",
      group.title.length <= 200 ? group.title : undefined,
    ),
    enabled: group.coverUrl === null && coverVisible &&
      coverConnectorId !== undefined && coverRemoteId !== undefined,
    retry: false,
    staleTime: Infinity,
  });
  const coverUrl = group.coverUrl ?? coverQuery.data?.data.coverUrl ?? null;
  const coverLoading = group.coverUrl === null && coverQuery.isFetching;

  return (
    <li className={styles.resultItem}>
      <article className={styles.resultCard}>
        <div aria-label={coverTitle} className={styles.coverFrame} ref={coverFrameRef}>
          {coverUrl !== null && !coverFailed ? (
            <img
              alt={coverTitle}
              loading="lazy"
              onError={() => setCoverFailed(true)}
              referrerPolicy="no-referrer"
              src={coverUrl}
            />
          ) : (
            <span>{coverLoading ? "Buscando portada…" : "Sin portada"}</span>
          )}
        </div>
        <div className={styles.resultDetails}>
          <h2>{group.title}</h2>
        </div>
        <div aria-label={`Fuentes de ${group.title}`} className={styles.sourceRows} role="group">
          {group.sources.map((entry) => {
            const sourceKey = `${group.key}|${entry.key}`;
            return (
              <SearchResultSource
                connector={entry.connector}
                expanded={expandedKey === sourceKey}
                key={entry.key}
                onToggle={() => onToggleSource(sourceKey)}
                result={entry.result}
                displayTitle={group.title}
              />
            );
          })}
        </div>
      </article>
    </li>
  );
}

export function SearchPage() {
  const [searchParams] = useSearchParams();
  const prefilledQuery = searchParams.get("q") ?? "";
  const [inputValue, setInputValue] = useState(prefilledQuery);
  const [submittedQuery, setSubmittedQuery] = useState<string | null>(
    prefilledQuery.trim() && prefilledQuery.length <= 200 ? prefilledQuery : null,
  );
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState("");
  const [validationError, setValidationError] = useState<string | null>(
    prefilledQuery.length > 200 ? "La búsqueda no puede superar 200 caracteres." : null,
  );
  useEffect(() => {
    const query = searchParams.get("q");
    if (query === null) return;
    setInputValue(query);
    setExpandedKey(null);
    setSourceFilter("");
    if (!query.trim()) {
      setSubmittedQuery(null);
      setValidationError(null);
    } else if (query.length > 200) {
      setSubmittedQuery(null);
      setValidationError("La búsqueda no puede superar 200 caracteres.");
    } else {
      setValidationError(null);
      setSubmittedQuery(query);
    }
  }, [searchParams]);

  const connectorsQuery = useQuery({
    queryKey: ["connectors"],
    queryFn: getConnectors,
  });
  const connectors = connectorsQuery.isError ? [] : connectorsQuery.data?.data ?? [];
  const capableConnectors = connectors.filter(supportsSearch);
  const connectorSearchQueries = useQueries({
    queries: capableConnectors.map((connector) => {
      const pageBudget = 3;
      return {
        queryKey: ["connector-search", connector.connectorId, submittedQuery, pageBudget],
        queryFn: ({ signal }: { signal: AbortSignal }) => getConnectorSearch(
          connector.connectorId,
          submittedQuery ?? "",
          pageBudget,
          signal,
        ),
        enabled: submittedQuery !== null,
        retry: false,
      };
    }),
  });
  const isSearchPending = submittedQuery !== null &&
    connectorSearchQueries.some((query) => query.isPending || query.isFetching);
  const searchOutcomes = capableConnectors.map((connector, index) => ({
    connector,
    query: connectorSearchQueries[index],
    response: connectorSearchQueries[index]?.data as ConnectorSearchResponse | undefined,
    error: connectorSearchQueries[index]?.isError
      ? connectorSearchQueries[index].error
      : null,
  }));
  const sourceResults = searchOutcomes.flatMap((outcome) =>
    outcome.response?.data
      .slice(0, MAX_SEARCH_RESULTS_PER_SOURCE)
      .map((result, index) => ({
        connector: outcome.connector,
        result,
        key: `${outcome.connector.connectorId}:${result.remoteId ?? result.remoteUrl ?? "result"}:${index}`,
      })) ?? [],
  );
  const availableSources = [...new Set(sourceResults.map((entry) =>
    resultSourceLabel(entry.result, entry.connector.connectorId),
  ))].sort((left, right) => left.localeCompare(right, "es"));
  const searchResults = groupSearchResults(sourceResults.filter((entry) =>
    !sourceFilter || resultSourceLabel(entry.result, entry.connector.connectorId) === sourceFilter,
  ));
  const searchFailures = searchOutcomes.filter((outcome) => outcome.error !== null);
  const truncatedSourceCount = searchOutcomes.filter(
    (outcome) => (outcome.response?.data.length ?? 0) > MAX_SEARCH_RESULTS_PER_SOURCE,
  ).length;
  const successfulSourceCount = searchOutcomes.filter((outcome) => outcome.response !== undefined).length;
  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = inputValue;
    setExpandedKey(null);
    setSourceFilter("");
    if (!query.trim()) {
      setSubmittedQuery(null);
      setValidationError("Escribe un título para buscar.");
      return;
    }
    if (query.length > 200) {
      setSubmittedQuery(null);
      setValidationError("La búsqueda no puede superar 200 caracteres.");
      return;
    }
    if (capableConnectors.length === 0) {
      setSubmittedQuery(null);
      setValidationError("No hay fuentes disponibles para buscar.");
      return;
    }
    setValidationError(null);
    setSubmittedQuery(query);
  }

  return (
    <section aria-labelledby="search-title" className={styles.page}>
      <header className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>Catálogo unificado</p>
          <h1 id="search-title">Buscar</h1>
          <p className={styles.intro}>
            Busca una vez en todas las fuentes. Cada tarjeta reúne la portada del manga y muestra qué sitios permiten descargarlo; pulsa un sitio para ver sus capítulos.
          </p>
        </div>
      </header>

      {connectorsQuery.isPending ? (
        <p aria-live="polite" className={styles.statusMessage} role="status">
          Cargando fuentes registradas…
        </p>
      ) : connectorsQuery.isError ? (
        <SearchError
          error={connectorsQuery.error}
          onRetry={() => void connectorsQuery.refetch()}
          retryLabel="Reintentar lista de fuentes"
        />
      ) : connectors.length === 0 ? (
        <p className={styles.statusMessage} role="status">
          No hay conectores registrados en el catálogo local.
        </p>
      ) : capableConnectors.length === 0 ? (
        <p className={styles.statusMessage} role="status">
          Ningún conector registrado ofrece búsqueda.
        </p>
      ) : null}

      <form className={styles.searchForm} onSubmit={handleSubmit} role="search">
        <div className={styles.searchField}>
          <label htmlFor="connector-search-query">Título o palabras clave</label>
          <input
            autoComplete="off"
            disabled={capableConnectors.length === 0}
            id="connector-search-query"
            name="query"
            onChange={(event) => setInputValue(event.currentTarget.value)}
            placeholder="Por ejemplo: fixture"
            role="searchbox"
            type="search"
            value={inputValue}
          />
        </div>
        <button
          className={styles.primaryButton}
          disabled={capableConnectors.length === 0 || isSearchPending}
          type="submit"
        >
          {isSearchPending ? "Buscando…" : "Buscar"}
        </button>
      </form>

      {availableSources.length > 1 ? (
        <div className={styles.searchField}>
          <label htmlFor="search-source-filter">Mostrar resultados de</label>
          <select
            id="search-source-filter"
            value={sourceFilter}
            onChange={(event) => {
              setSourceFilter(event.currentTarget.value);
              setExpandedKey(null);
            }}
          >
            <option value="">Todas las fuentes</option>
            {availableSources.map((source) => (
              <option key={source} value={source}>
                {source} ({sourceResults.filter((entry) =>
                  resultSourceLabel(entry.result, entry.connector.connectorId) === source,
                ).length})
              </option>
            ))}
          </select>
        </div>
      ) : null}

      {validationError ? (
        <p className={styles.validationError} role="alert">
          {validationError}
        </p>
      ) : null}

      {submittedQuery === null ? (
        capableConnectors.length === 0 || connectorsQuery.isPending || connectorsQuery.isError ? null : (
          <p className={styles.statusMessage} role="status">
            Escribe un título para buscar en todas las fuentes.
          </p>
        )
      ) : searchResults.length === 0 && searchFailures.length === 0 && isSearchPending ? (
        <p aria-live="polite" className={styles.statusMessage} role="status">
          Buscando “{submittedQuery}” en todas las fuentes…
        </p>
      ) : searchResults.length === 0 && searchFailures.length === 0 && !isSearchPending ? (
        <div className={styles.searchEmptyState}>
          <p className={styles.statusMessage} role="status">
            No se encontraron series para “{submittedQuery}” en las fuentes consultadas.
          </p>
        </div>
      ) : (
        <>
          {searchFailures.length > 0 ? (
            <section aria-label="Fuentes con errores de búsqueda" className={styles.sourceFailures}>
              <p className={styles.statusMessage}>
                {searchResults.length > 0
                  ? "Algunas fuentes no respondieron; se muestran los demás resultados."
                  : isSearchPending
                    ? "Algunas fuentes fallaron; se siguen consultando las demás."
                    : successfulSourceCount > 0
                      ? "Algunas fuentes fallaron; se muestran los resultados de las que respondieron."
                      : "No se pudo consultar ninguna fuente correctamente."}
              </p>
              {searchFailures.map(({ connector, error, query }) => (
                <div className={styles.sourceFailure} key={connector.connectorId}>
                  <p>{connectorLabel(connector.connectorId)}</p>
                  <SearchError
                    error={error}
                    onRetry={() => void query?.refetch()}
                    retryLabel="Reintentar fuente"
                  />
                </div>
              ))}
            </section>
          ) : null}
          {isSearchPending && searchResults.length > 0 ? (
            <p aria-live="polite" className={styles.statusMessage} role="status">
              Se muestran resultados parciales mientras responden las otras fuentes…
            </p>
          ) : null}
          {searchResults.length === 0 && !isSearchPending && successfulSourceCount > 0 ? (
            <p className={styles.statusMessage} role="status">
              Las fuentes que respondieron no encontraron “{submittedQuery}”.
            </p>
          ) : null}
          {searchResults.length > 0 ? (
            <section aria-labelledby="search-results-title" className={styles.resultsSection}>
              <div className={styles.resultsHeading}>
                <h2 id="search-results-title">Resultados</h2>
                <p className={styles.statusMessage} role="status">
                  {searchResults.length} {searchResults.length === 1 ? "resultado" : "resultados"}
                </p>
              </div>
              {truncatedSourceCount > 0 ? (
                <p className={styles.statusMessage} role="status">
                  Se muestran como máximo {MAX_SEARCH_RESULTS_PER_SOURCE} resultados por fuente.
                </p>
              ) : null}
              <ul className={styles.resultList}>
                {searchResults.map((group) => (
                  <SearchResultCard
                    expandedKey={expandedKey}
                    group={group}
                    key={group.key}
                    onToggleSource={(sourceKey) => setExpandedKey((current) =>
                      current === sourceKey ? null : sourceKey,
                    )}
                  />
                ))}
              </ul>
            </section>
          ) : null}
        </>
      )}
    </section>
  );
}
