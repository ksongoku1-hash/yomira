import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Component, useState } from "react";
import type { ReactNode } from "react";

import {
  ApiError,
  getLibraryPage,
  getLocalImportStatus,
  openLibraryFolder,
  type LibraryReadingStatus,
  type LibrarySeriesSummary,
} from "../api/client";
import styles from "./LibraryPage.module.css";
import { SeriesCover } from "./SeriesCover";
import { publicationStatus } from "./publicationStatus";

const PAGE_SIZE = 100;
const READING_FILTERS: { label: string; value: "all" | LibraryReadingStatus }[] = [
  { label: "Todos", value: "all" },
  { label: "Leyendo", value: "reading" },
  { label: "Completos", value: "completed" },
  { label: "Incompletos", value: "incomplete" },
];

function LibraryLoading() {
  return (
    <div aria-label="Cargando biblioteca" className={styles.loadingRegion}>
      <p aria-live="polite" className={styles.statusMessage} role="status">
        Cargando biblioteca…
      </p>
      <div className={styles.seriesGrid}>
        {Array.from({ length: 8 }, (_, index) => (
          <div aria-hidden="true" className={styles.skeletonCard} key={index} />
        ))}
      </div>
    </div>
  );
}

function LibraryErrorState({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const apiError = error instanceof ApiError ? error : null;

  return (
    <div aria-labelledby="library-error-title" className={styles.errorState} role="alert">
      <div>
        <h2 id="library-error-title">No se pudo cargar la biblioteca</h2>
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
        Reintentar
      </button>
    </div>
  );
}

function SeriesCard({ series }: { series: LibrarySeriesSummary }) {
  return (
    <li className={styles.seriesItem}>
      <article aria-labelledby={`series-${series.seriesId}`} className={styles.seriesCard}>
        <Link className={styles.coverLink} aria-label={`Abrir ${series.displayTitle}`} to={`/series/${series.seriesId}`}>
          <SeriesCover series={series} />
        </Link>
        <div className={styles.cardDetails}>
          <h2 className={styles.seriesTitle} id={`series-${series.seriesId}`}>
            <Link className={styles.seriesLink} to={`/series/${series.seriesId}`}>
              {series.displayTitle}
            </Link>
          </h2>
          {series.description ? <p className={styles.description}>{series.description}</p> : null}
          <div className={styles.metadata}>
            {series.author !== null ? <span>Autor: {series.author || "Sin asignar"}</span> : null}
            {series.genres !== null ? (
              <span>Géneros: {series.genres.length ? series.genres.join(", ") : "Sin asignar"}</span>
            ) : null}
            {series.language ? <span>Idioma: {series.language}</span> : null}
            {series.status ? <span>Publicación: {publicationStatus(series.status)}</span> : null}
          </div>
        </div>
      </article>
    </li>
  );
}

export function LocalImportPanel({ compact = false }: { compact?: boolean }) {
  const [folderMessage, setFolderMessage] = useState("");
  const query = useQuery({
    queryKey: ["local-imports"],
    queryFn: () => getLocalImportStatus(),
    refetchInterval: 2000,
  });
  const retry = useMutation({
    mutationFn: () => getLocalImportStatus(true),
    onSuccess: () => { void query.refetch(); },
  });
  const open = useMutation({
    mutationFn: openLibraryFolder,
    onMutate: () => setFolderMessage("Abriendo carpeta en este equipo…"),
    onSuccess: () => setFolderMessage("Carpeta abierta en el Explorador de este equipo."),
    onError: (error) => setFolderMessage(error instanceof ApiError ? error.message :
      "No se pudo abrir la carpeta. Copia su ruta y ábrela desde el Explorador."),
  });
  async function copyPath(path: string) {
    try {
      await navigator.clipboard.writeText(path);
      setFolderMessage("Ruta copiada. Puedes pegarla en la barra de direcciones del Explorador.");
    } catch {
      setFolderMessage("El navegador no permitió copiar. Selecciona la ruta que aparece arriba y cópiala manualmente.");
    }
  }
  const labels = { library: "Carpeta principal", inbox: "Para añadir mangas",
    downloads: "Capítulos preparados por Yomira", legacy: "Carpeta anterior reconocida" };
  const folders = query.data?.folders.length ? query.data.folders : query.data ? [
    { kind: "library" as const, path: query.data.folder, problem: null },
  ] : [];
  const mainFolderProblem = folders.find((folder) => folder.kind === "library")?.problem;
  const legacyFolders = folders.filter((folder) => folder.kind === "legacy");
  const folderCard = (folder: typeof folders[number]) => (
    <section className={styles.folderCard} key={`${folder.kind}:${folder.path}`} aria-label={labels[folder.kind]}>
      <h3>{labels[folder.kind]}</h3>
      <p className={styles.folderPath}>{folder.path}</p>
      <p className={folder.problem ? styles.folderWarning : styles.helpText} role={folder.problem ? "alert" : undefined}>
        {folder.problem ?? "Carpeta disponible."}
      </p>
      <div className={styles.folderActions}>
        {folder.kind !== "legacy" ? <button className={styles.secondaryButton}
          disabled={open.isPending || Boolean(folder.problem && (folder.kind !== "inbox" || mainFolderProblem))} type="button"
          aria-label={`Abrir ${labels[folder.kind].toLowerCase()}`}
          onClick={() => open.mutate(folder.kind as "library" | "inbox" | "downloads")}>
          {folder.kind === "inbox" && folder.problem ? "Preparar carpeta" : "Abrir carpeta"}
        </button> : null}
        <button className={styles.secondaryButton} type="button" aria-label={`Copiar ruta de ${labels[folder.kind].toLowerCase()}`}
          onClick={() => { void copyPath(folder.path); }}>Copiar ruta</button>
      </div>
    </section>
  );
  return (
    <aside className={styles.importPanel} aria-labelledby="local-import-title">
      <h2 id="local-import-title">Carpetas de tus mangas</h2>
      <p>Las carpetas y los archivos están en el equipo donde se ejecuta Yomira.</p>
      {query.isPending ? <p role="status">Comprobando carpetas…</p> : null}
      <div className={styles.folderGrid}>
        {folders.filter((folder) => folder.kind !== "legacy" && (!compact || folder.kind === "inbox" ||
          (folders.length === 1 && folder.kind === "library"))).map(folderCard)}
      </div>
      {!compact && legacyFolders.length ? <details className={styles.folderGuide}>
        <summary>Carpetas anteriores que se siguen reconociendo ({legacyFolders.length})</summary>
        <div className={styles.folderGrid}>{legacyFolders.map(folderCard)}</div>
      </details> : null}
      {folderMessage ? <p role="status" aria-live="polite">{folderMessage}</p> : null}
      {compact ? <p><Link to="/settings">Gestionar todas las carpetas en Ajustes</Link></p> : null}
      <button className={styles.textButton} type="button" disabled={query.isFetching} onClick={() => { void query.refetch(); }}>Comprobar carpetas de nuevo</button>
      <details className={styles.folderGuide}>
        <summary>Cómo añadir mangas y cuidar tus archivos</summary>
        <p>Copia tus mangas dentro de «Para añadir», con una carpeta por manga. Yomira prepara una copia para el lector y busca su portada y metadatos; conserva tus archivos originales.</p>
        <pre className={styles.folderExample}>{"Para añadir/\n  Naruto/\n    Capítulo 001.epub\n    Capítulo 002.cbz\n    Capítulo 003/\n      001.jpg\n      002.jpg"}</pre>
        <p>Admite imágenes PNG, JPEG o WebP, CBZ/ZIP y EPUB de Free Manga. Espera a que termine la copia. Si usas una carpeta sincronizada, los archivos deben estar descargados en este equipo.</p>
        <p>Yomira sigue reconociendo los mangas que ya dejaste en la carpeta principal y en las carpetas anteriores mostradas arriba.</p>
        <p>Gestiona los capítulos desde Yomira: cambiar de nombre o mover los archivos preparados fuera de su carpeta puede impedir que se abran en el lector.</p>
        <p>Si moviste la carpeta principal, vuelve a colocarla en la ruta indicada y reinicia Yomira. La aplicación evita escribir en una carpeta que haya sido sustituida.</p>
        <p>Si un título coincide con varias obras, abre su ficha y elige los metadatos correctos.</p>
      </details>
      {query.data?.scanning ? <p role="status">Comprobando archivos… {query.data.importedChapters} capítulos incorporados en esta sesión.</p> : null}
      {query.data && query.data.pendingMetadata > 0 ? <p role="status">Metadatos en cola o en búsqueda: {query.data.pendingMetadata} manga(s).</p> : null}
      {query.data && query.data.metadataReview > 0 ? <p>La búsqueda terminó. {query.data.metadataReview} manga(s) necesitan revisión de metadatos; abre su ficha para elegir una coincidencia o completar la información.</p> : null}
      {query.isError ? <p role="alert">No se pudo comprobar la importación automática. <button className={styles.textButton} type="button" onClick={() => { void query.refetch(); }}>Comprobar de nuevo</button></p> : null}
      {query.data?.errors.length ? (
        <div role="alert">
          <p>Estos archivos necesitan revisión:</p>
          <ul>{query.data.errors.map((error) => <li key={error.file}><strong>{error.file}</strong>: {error.message}
            {error.chapterId ? <p>Ya tienes una copia de este capítulo disponible en la biblioteca. <Link to={`/reader/${error.chapterId}`}>Leer la copia disponible</Link>. El archivo original se conserva.</p> : null}
          </li>)}</ul>
          <button className={styles.secondaryButton} disabled={retry.isPending} type="button" onClick={() => retry.mutate()}>Reintentar archivos</button>
          {retry.isError ? <p>No se pudo solicitar el reintento.</p> : null}
        </div>
      ) : null}
    </aside>
  );
}

function LibraryPageContent() {
  const [offset, setOffset] = useState(0);
  const [searchTerm, setSearchTerm] = useState("");
  const [readingFilter, setReadingFilter] = useState<"all" | LibraryReadingStatus>("all");
  const query = useQuery({
    queryKey: ["library", readingFilter, offset],
    queryFn: () => getLibraryPage({
      limit: PAGE_SIZE,
      offset,
      ...(readingFilter === "all" ? {} : { readingStatus: readingFilter }),
    }),
    placeholderData: (previousData, previousQuery) =>
      previousQuery?.queryKey[1] === readingFilter ? previousData : undefined,
  });
  const page = query.data;
  const normalizedSearch = searchTerm.trim().toLowerCase();
  const visibleSeries = page?.data.filter((series) => {
    if (!normalizedSearch) return true;
    return (
      series.displayTitle.toLowerCase().includes(normalizedSearch) ||
      series.canonicalTitle.toLowerCase().includes(normalizedSearch)
    );
  });
  const hasPrevious = page !== undefined && page.meta.offset > 0;
  const hasNext =
    page !== undefined && page.meta.offset + page.data.length < page.meta.total;
  const totalPages = page === undefined ? 0 : Math.ceil(page.meta.total / page.meta.limit);
  const currentPage = page === undefined ? 0 : Math.floor(page.meta.offset / page.meta.limit) + 1;

  const refresh = () => {
    void query.refetch();
  };

  return (
    <section
      aria-busy={query.isFetching}
      aria-labelledby="library-title"
      className={styles.page}
    >
      <header className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>Tu colección local</p>
          <h1 id="library-title">Biblioteca</h1>
          <p className={styles.intro}>Tus series, organizadas en un solo lugar.</p>
        </div>
        <button
          className={styles.secondaryButton}
          disabled={query.isFetching}
          onClick={refresh}
          type="button"
        >
          Actualizar biblioteca
        </button>
      </header>

      <div className={styles.toolbar}>
        <div className={styles.filterControls}>
          <span className={styles.filterLabel}>Estado de lectura</span>
          <div aria-label="Filtrar por estado de lectura" className={styles.filterGroup} role="group">
            {READING_FILTERS.map((filter) => (
              <button
                aria-pressed={readingFilter === filter.value}
                className={readingFilter === filter.value
                  ? `${styles.filterButton} ${styles.filterButtonActive}`
                  : styles.filterButton}
                key={filter.value}
                onClick={() => {
                  if (readingFilter !== filter.value) {
                    setReadingFilter(filter.value);
                    setOffset(0);
                  }
                }}
                type="button"
              >
                {filter.label}
              </button>
            ))}
          </div>
          <p className={styles.helpText}>
            Se calcula con los capítulos y el progreso guardados localmente. Completos significa que
            todos los capítulos conocidos están al 100 %.
          </p>
        </div>
        <div className={styles.searchField}>
          <label htmlFor="library-search">Buscar título en esta página</label>
          <input
            aria-describedby="library-search-help"
            autoComplete="off"
            id="library-search"
            onChange={(event) => setSearchTerm(event.currentTarget.value)}
            placeholder="Escribe un título"
            type="search"
            value={searchTerm}
          />
          <p className={styles.helpText} id="library-search-help">
            Busca entre los títulos cargados; cambia de página para revisar el resto.
          </p>
        </div>
      </div>

      {query.isPending && page === undefined ? <LibraryLoading /> : null}

      {query.isError ? <LibraryErrorState error={query.error} onRetry={refresh} /> : null}

      {query.isFetching && query.isPlaceholderData ? (
        <p aria-live="polite" className={styles.statusMessage} role="status">
          Cargando otra página; se conserva la lista anterior mientras tanto.
        </p>
      ) : null}

      {page && page.meta.total === 0 && readingFilter !== "all" ? (
        <div className={styles.emptyState}>
          <span aria-hidden="true" className={styles.emptyMark} />
          <h2>No hay series con este filtro.</h2>
          <p>Prueba otro estado de lectura o vuelve a ver todas las series locales.</p>
          <button
            className={styles.secondaryButton}
            onClick={() => {
              setReadingFilter("all");
              setOffset(0);
            }}
            type="button"
          >
            Mostrar todas
          </button>
        </div>
      ) : null}

      {page && page.meta.total === 0 && readingFilter === "all" ? (
        <div className={styles.emptyState}>
          <span aria-hidden="true" className={styles.emptyMark} />
          <h2>Tu biblioteca está vacía.</h2>
          <p>Las series aparecerán aquí cuando se añadan a tu biblioteca local.</p>
          <button
            className={styles.secondaryButton}
            disabled={query.isFetching}
            onClick={refresh}
            type="button"
          >
            Actualizar lista
          </button>
        </div>
      ) : null}

      {page && page.meta.total > 0 ? (
        <>
          <div className={styles.listHeader}>
            <p aria-live="polite" className={styles.rangeText} role="status">
              Mostrando {page.data.length === 0 ? 0 : page.meta.offset + 1}–
              {page.meta.offset + page.data.length} de {page.meta.total} series
              {normalizedSearch ? ` · ${visibleSeries?.length ?? 0} coincidencias en esta página` : ""}
            </p>
          </div>

          {visibleSeries?.length ? (
            <ul aria-label="Series de la biblioteca" className={styles.seriesGrid}>
              {visibleSeries.map((series) => (
                <SeriesCard key={series.seriesId} series={series} />
              ))}
            </ul>
          ) : (
            <div aria-live="polite" className={styles.noMatches}>
              <h2>No hay coincidencias en esta página.</h2>
              <p>Prueba otro título o cambia de página para revisar más series.</p>
              <button
                className={styles.textButton}
                onClick={() => setSearchTerm("")}
                type="button"
              >
                Limpiar búsqueda
              </button>
            </div>
          )}

          {totalPages > 1 ? (
            <nav aria-label="Paginación de biblioteca" className={styles.pagination}>
              <button
                aria-label="Página anterior"
                className={styles.secondaryButton}
                disabled={!hasPrevious || query.isFetching}
                onClick={() => setOffset(Math.max(0, page.meta.offset - page.meta.limit))}
                type="button"
              >
                Anterior
              </button>
              <span aria-live="polite" className={styles.pageNumber}>
                Página {currentPage} de {totalPages}
              </span>
              <button
                aria-label="Página siguiente"
                className={styles.secondaryButton}
                disabled={!hasNext || query.isFetching}
                onClick={() => setOffset(page.meta.offset + page.meta.limit)}
                type="button"
              >
                Siguiente
              </button>
            </nav>
          ) : null}
        </>
      ) : null}
      {page ? <LocalImportPanel compact /> : null}
    </section>
  );
}

class LibraryErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError(): { hasError: boolean } {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return (
        <section aria-labelledby="library-boundary-title" className={styles.boundaryState}>
          <h1 id="library-boundary-title">La biblioteca tuvo un problema</h1>
          <p>La navegación principal sigue disponible. Puedes volver a cargar esta sección.</p>
          <button
            className={styles.secondaryButton}
            onClick={() => this.setState({ hasError: false })}
            type="button"
          >
            Volver a intentar
          </button>
        </section>
      );
    }
    return this.props.children;
  }
}

export function LibraryPage() {
  return (
    <LibraryErrorBoundary>
      <LibraryPageContent />
    </LibraryErrorBoundary>
  );
}
