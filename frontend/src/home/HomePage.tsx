import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  ApiError, getRecentLibrary, getRecentReaderProgress, getSystemReadiness, getSeriesCoverUrl,
  type LibrarySeriesSummary, type RecentReaderProgress,
} from "../api/client";
import { ActionIcon } from "../app/ActionIcon";
import { useAppearanceStore } from "../app/appearanceStore";
import { SeriesCover } from "../library/SeriesCover";
import { IllustratedBackdrop } from "../library/IllustratedBackdrop";
import { publicationStatus } from "../library/publicationStatus";
import styles from "./HomePage.module.css";
import { OnlineRecommendations } from "./OnlineRecommendations";
import { ProgressPercentage } from "./ProgressPercentage";

const RECENT_LIMIT = 4;

function ProgressCover({ progress }: { progress: RecentReaderProgress }) {
  const [failedSource, setFailedSource] = useState<string | null>(null);
  const source = getSeriesCoverUrl(progress.seriesId, "medium", progress.updatedAt);
  return source === failedSource ? <div className={styles.coverPlaceholder} role="img" aria-label={`Portada no disponible: ${progress.seriesDisplayTitle}`}>
    <img src="/assets/brand/yomira-ice-mark.svg" alt="" width="48" height="48" /><span>{progress.seriesDisplayTitle}</span>
  </div> : <img className={styles.progressCover} src={source} alt={`Portada de ${progress.seriesDisplayTitle}`}
    loading="lazy" decoding="async" onError={() => setFailedSource(source)} />;
}

function errorDetails(error: unknown) {
  return error instanceof ApiError ? { message: error.message, code: error.code ?? "Sin código de servidor",
    status: error.status, traceId: error.traceId, retryable: error.retryable }
    : { message: "Falló una operación de la interfaz.", code: "Sin código de servidor", status: null, traceId: null, retryable: false };
}

function RecentError({ error, label, onRetry, retryLabel, retryDisabled, titleId }: {
  error: unknown; label: string; onRetry: () => void; retryLabel: string; retryDisabled: boolean; titleId: string;
}) {
  const details = errorDetails(error);
  return <div aria-labelledby={titleId} className={styles.sectionError} role="alert">
    <div><h3 id={titleId}>{label}</h3><p>{details.message}</p>
      <p className={styles.errorDetails}>Código: <code>{details.code}</code>{details.status !== null ? ` · HTTP ${details.status}` : null}</p>
      {details.traceId ? <p className={styles.errorDetails}>Traza: <code>{details.traceId}</code></p> : null}</div>
    {details.retryable ? <button className={styles.secondaryAction} disabled={retryDisabled} onClick={onRetry} type="button">{retryLabel}</button> : null}
  </div>;
}

function LoadingCards({ label, reading = false }: { label: string; reading?: boolean }) {
  return <div role="status" aria-label={label}><span className={styles.srOnly}>{label}</span><div className={styles.cardRow} aria-hidden="true">
    {Array.from({ length: 4 }, (_, index) => <div key={index} className={`${styles.skeletonCard} ${reading ? styles.readingSkeleton : ""}`}><div /><span /><span /></div>)}
  </div></div>;
}

function WorkCard({ series, reason }: { series: LibrarySeriesSummary; reason?: string }) {
  return <li className={styles.workItem} data-motion-card><article>
    <div className={styles.workCover}>
      <Link aria-label={`Abrir ficha de ${series.displayTitle}`} className={styles.seriesLink} to={`/series/${series.seriesId}`}>
        <SeriesCover series={series} className={styles.recentCover} variant="medium" />
      </Link>
      <div className={styles.quickActions}>
        <Link className={styles.primaryAction} to={`/series/${series.seriesId}#chapters`} aria-label={`Leer ${series.displayTitle}: elegir capítulo`}><ActionIcon name="play" />Leer</Link>
        <button className={styles.squareAction} type="button" disabled aria-label={`${series.displayTitle} ya está en tu biblioteca`} title="En tu biblioteca"><ActionIcon name="check" /></button>
      </div>
    </div>
    <Link className={styles.itemTitle} title={series.displayTitle} to={`/series/${series.seriesId}`}>{series.displayTitle}</Link>
    <span className={styles.itemMetadata}>{series.genres?.slice(0, 2).join(" · ") || series.author || "En tu biblioteca"}</span>
    {reason ? <span className={styles.recommendationReason}>{reason}</span> : null}
  </article></li>;
}

interface FeaturedWork { id: string; title: string; series: LibrarySeriesSummary | undefined; progress: RecentReaderProgress | undefined }

export function HomePage() {
  const [progressFilter, setProgressFilter] = useState<"all" | "live" | "end">("all");
  const [activeSlide, setActiveSlide] = useState(0);
  const [paused, setPaused] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [visible, setVisible] = useState(() => !document.hidden);
  const [reducedMotion, setReducedMotion] = useState(() => typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const simpleGraphics = useAppearanceStore((state) => state.simpleGraphics);
  useEffect(() => {
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const updateMotion = () => setReducedMotion(media?.matches ?? false);
    const updateVisibility = () => setVisible(!document.hidden);
    media?.addEventListener?.("change", updateMotion);
    document.addEventListener("visibilitychange", updateVisibility);
    return () => { media?.removeEventListener?.("change", updateMotion); document.removeEventListener("visibilitychange", updateVisibility); };
  }, []);
  const readiness = useQuery({ queryKey: ["system", "readiness"], queryFn: getSystemReadiness });
  const serviceReady = readiness.isSuccess && readiness.data.status === "ready";
  const recentLibrary = useQuery({ queryKey: ["library", "recent", RECENT_LIMIT], queryFn: () => getRecentLibrary(RECENT_LIMIT), enabled: serviceReady });
  const recentProgress = useQuery({ queryKey: ["reader", "progress", "recent", RECENT_LIMIT, "series"],
    queryFn: () => getRecentReaderProgress(RECENT_LIMIT, true), enabled: serviceReady,
    refetchOnMount: "always", refetchOnWindowFocus: "always" });
  const recentWorks = useMemo(() => recentLibrary.data?.data.slice(0, RECENT_LIMIT) ?? [], [recentLibrary.data]);
  const reading = useMemo(() => recentProgress.data?.data.slice(0, RECENT_LIMIT) ?? [], [recentProgress.data]);
  const hasResume = reading.some((item) => Number(item.percentage) < 100);
  const slides = useMemo(() => {
    const resume = reading.find((item) => Number(item.percentage) < 100);
    const first: FeaturedWork[] = resume ? [{ id: resume.seriesId, title: resume.seriesDisplayTitle, progress: resume,
      series: recentWorks.find((series) => series.seriesId === resume.seriesId) }] : [];
    return [...first, ...recentWorks.filter((series) => series.seriesId !== resume?.seriesId).map((series) => ({
      id: series.seriesId, title: series.displayTitle, series,
      progress: reading.find((item) => item.seriesId === series.seriesId && Number(item.percentage) < 100),
    }))].slice(0, 4);
  }, [recentWorks, reading]);
  useEffect(() => {
    if (slides.length < 2 || paused || hovered || focused || reducedMotion || simpleGraphics || !visible) return;
    const timer = window.setInterval(() => setActiveSlide((current) => (current + 1) % slides.length), 8000);
    return () => window.clearInterval(timer);
  }, [slides.length, paused, hovered, focused, reducedMotion, simpleGraphics, visible]);
  const index = slides.length ? activeSlide % slides.length : 0;
  const featured = slides[index];
  const progress = featured?.progress;
  const backgroundSource = featured ? getSeriesCoverUrl(featured.id, "medium", progress?.updatedAt ?? featured.series?.updatedAt) : null;
  const filteredProgress = reading.filter((item) => progressFilter === "all" ||
    (progressFilter === "end" ? Number(item.percentage) >= 100 : Number(item.percentage) < 100));
  const readIds = new Set(reading.map((item) => item.seriesId));
  const readGenres = recentWorks.filter((series) => readIds.has(series.seriesId)).flatMap((series) => series.genres ?? []);
  const libraryLoading = !recentLibrary.data && (readiness.isPending || (serviceReady && recentLibrary.isPending));
  const progressLoading = !recentProgress.data && (readiness.isPending || (serviceReady && recentProgress.isPending));

  return <div className={styles.page}>
    {!serviceReady ? <aside className={styles.serviceCard} aria-labelledby="service-title"><h2 id="service-title">Servicio local</h2>
      {readiness.isPending ? <p role="status">Comprobando preparación del servicio local…</p> : null}
      {readiness.isError ? <RecentError error={readiness.error} label="No se pudo comprobar el servicio local" titleId="readiness-error-title"
        onRetry={() => void readiness.refetch()} retryLabel="Comprobar de nuevo" retryDisabled={readiness.isFetching} /> : null}
      {readiness.data?.status === "not_ready" ? <><p>El servicio local no está listo.</p><Link to="/diagnostics" className={styles.libraryLink}>Ver diagnóstico</Link>
        <button type="button" className={styles.secondaryAction} disabled={readiness.isFetching} onClick={() => void readiness.refetch()}>Comprobar de nuevo</button></> : null}
      {reading.length || recentWorks.length ? <p>Tu biblioteca y tu progreso guardados se conservan. Puedes volver a comprobar el servicio.</p> : null}
    </aside> : null}

    <header className={styles.discoveryHeader}>
      <div><p className={styles.eyebrow}>Para ti</p><h1 id="home-title">Descubre tu próxima historia</h1>
        <p>Explora nuevas obras y vuelve a las tuyas cuando quieras.</p></div>
      <div className={styles.discoveryActions}>
        {hasResume ? <a className={styles.primaryAction} href="#recent-progress-title"><ActionIcon name="play" />Continuar leyendo</a> : null}
        <Link className={styles.secondaryAction} to="/library"><ActionIcon name="grid" />Mi biblioteca</Link>
      </div>
    </header>
    <OnlineRecommendations kind="personal" enabled={serviceReady} readingTitles={reading.map((item)=>item.seriesDisplayTitle)} readingGenres={readGenres} />

    <section aria-labelledby="recent-progress-title" className={styles.recentSection} data-motion-reveal>
      <header className={styles.sectionHeader}><div><h2 id="recent-progress-title"><span className={styles.sectionNumber} aria-hidden="true" />Continuar leyendo</h2><p>Retoma exactamente donde lo dejaste.</p></div>
        <div className={styles.sectionTools}>
          <div className={styles.progressFilters} role="group" aria-label="Filtrar lecturas">{([ ["all", "Todo"], ["live", "En curso"], ["end", "Terminados"] ] as const).map(([filter, label]) =>
            <button key={filter} type="button" aria-pressed={progressFilter === filter} onClick={() => setProgressFilter(filter)}>{label}</button>)}</div>
          <Link className={styles.libraryLink} to="/library?status=reading">Ver historial<ActionIcon name="next" /></Link>
        </div>
      </header>
      {progressLoading ? <LoadingCards label="Cargando progreso guardado…" reading /> : null}
      {recentProgress.isError ? <RecentError error={recentProgress.error} label="Error en progreso reciente" titleId="recent-progress-error-title"
        onRetry={() => void recentProgress.refetch()} retryLabel="Reintentar progreso reciente" retryDisabled={recentProgress.isFetching} /> : null}
      {!serviceReady && !reading.length && !progressLoading ? <p role="status" className={styles.statusMessage}>El progreso reciente estará disponible cuando el servicio local esté listo.</p> : null}
      {serviceReady && recentProgress.isSuccess && !reading.length ? <div className={styles.emptyState}><ActionIcon name="page" /><p>El progreso guardado de tus capítulos aparecerá aquí.</p></div> : null}
      {reading.length && !filteredProgress.length ? <p className={styles.emptyState} role="status">{progressFilter === "end" ? "No hay capítulos terminados entre tus lecturas recientes." : "No hay capítulos en curso entre tus lecturas recientes."}</p> : null}
      {filteredProgress.length ? <ul className={styles.continueList}>{filteredProgress.map((item) => <li key={item.chapterId} className={styles.recentItem} data-motion-card>
        <article className={styles.progressItem}>
          <span className={styles.cardProgress} aria-hidden="true"><ProgressPercentage value={Math.round(Number(item.percentage))} animate={!simpleGraphics && !reducedMotion} /><small>%</small></span>
          <Link aria-label={`Abrir ${item.seriesDisplayTitle}`} className={styles.progressChapterLink}
            to={Number(item.percentage) >= 100 ? `/series/${item.seriesId}` : `/reader/${item.chapterId}`} state={{ originatingSeriesId: item.seriesId }}><ProgressCover progress={item} /></Link>
          <div className={styles.progressInfo}>
            <Link className={styles.progressSeriesLink} aria-label={`Ver serie ${item.seriesDisplayTitle}`} to={`/series/${item.seriesId}`}>{item.seriesDisplayTitle}</Link>
            <p className={styles.progressPosition}>{item.chapterDisplayLabel} · Página {item.pageIndex + 1} · {item.percentage} %</p>
            <progress className={styles.readingBar} max={100} value={Number(item.percentage)} aria-label={`Progreso de ${item.chapterDisplayLabel} de ${item.seriesDisplayTitle}`} />
            <time className={styles.savedAt} dateTime={item.updatedAt}>{Number(item.percentage) >= 100 ? "Capítulo terminado · " : "Guardado "}{formatSavedAt(item.updatedAt)}</time>
            <div className={styles.progressActions}>
              <Link className={Number(item.percentage) >= 100 ? styles.secondaryAction : styles.primaryAction}
                to={Number(item.percentage) >= 100 ? `/series/${item.seriesId}` : `/reader/${item.chapterId}`} state={{ originatingSeriesId: item.seriesId }}
                aria-label={Number(item.percentage) >= 100 ? `Ver capítulos de ${item.seriesDisplayTitle}` : `Leer ${item.chapterDisplayLabel} de ${item.seriesDisplayTitle}`}>
                <ActionIcon name={Number(item.percentage) >= 100 ? "chapters" : "play"} />{Number(item.percentage) >= 100 ? "Capítulos" : "Continuar"}</Link>
              <Link className={styles.squareAction} to={Number(item.percentage) >= 100 ? `/reader/${item.chapterId}` : `/series/${item.seriesId}#chapters`}
                state={{ originatingSeriesId: item.seriesId }} aria-label={Number(item.percentage) >= 100 ? `Abrir ${item.chapterDisplayLabel} de ${item.seriesDisplayTitle}` : `Ver capítulos de ${item.seriesDisplayTitle}`} title={Number(item.percentage) >= 100 ? "Abrir capítulo" : "Elegir capítulo"}>
                <ActionIcon name={Number(item.percentage) >= 100 ? "refresh" : "chapters"} /></Link>
            </div>
          </div>
        </article>
      </li>)}</ul> : null}
    </section>

    <section className={styles.welcome} aria-label="Obras destacadas" onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
      onFocusCapture={() => setFocused(true)} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false); }}>
      {featured ? <div className={styles.heroSlide} key={featured.id}>
        <IllustratedBackdrop source={backgroundSource} />
        <div className={styles.welcomeCopy}>
          <p className={styles.eyebrow}>Destacado de tu biblioteca</p>
          <h2 id="featured-title">{featured.title}</h2>
          <div className={styles.tags}>
            {featured.series?.genres?.slice(0, 2).map((value) => <span key={value}>{value}</span>)}
            <span>{featured.series?.status ? publicationStatus(featured.series.status) : "En tu biblioteca"}</span>
          </div>
          <p className={styles.intro}>{featured.series?.description || (progress ? "Continúa desde la última página guardada." : "Una historia de tu colección, lista para explorar a tu ritmo.")}</p>
          {progress ? <div className={styles.heroReading}>
            <p><small>Donde lo dejaste</small><strong>{progress.chapterDisplayLabel}</strong><span>Página {progress.pageIndex + 1}</span></p>
            <div className={styles.heroBar}><b aria-hidden="true"><ProgressPercentage value={Math.round(Number(progress.percentage))} animate={!simpleGraphics && !reducedMotion} />%</b>
              <progress className={styles.readingBar} max={100} value={Number(progress.percentage)} aria-label={`Progreso de ${progress.chapterDisplayLabel}`} /></div>
          </div> : null}
          <div className={styles.heroActions}>
            {progress ? <Link className={styles.primaryAction} to={`/reader/${progress.chapterId}`} state={{ originatingSeriesId: featured.id }}
              aria-label={`Continuar lectura: ${progress.chapterDisplayLabel}`}><ActionIcon name="play" />Continuar lectura</Link>
              : <Link className={styles.primaryAction} to={`/series/${featured.id}#chapters`}><ActionIcon name="play" />Elegir capítulo</Link>}
            <Link className={styles.secondaryAction} to={`/series/${featured.id}`}><ActionIcon name="external" />Detalles</Link>
            <button className={styles.squareAction} type="button" disabled aria-label="Esta obra ya está en tu biblioteca" title="En tu biblioteca"><ActionIcon name="check" /></button>
            {progress ? <Link className={styles.squareAction} aria-label={`Elegir capítulo de ${featured.title}`} title="Elegir capítulo" to={`/series/${featured.id}#chapters`}><ActionIcon name="chapters" /></Link> : null}
          </div>
        </div>
        <Link className={styles.heroPoster}
          onPointerMove={(event) => {
            if (event.pointerType !== "mouse" || simpleGraphics || reducedMotion) return;
            const bounds = event.currentTarget.getBoundingClientRect();
            if (!bounds.width || !bounds.height) return;
            event.currentTarget.style.setProperty("--tilt-x", `${-(event.clientY - bounds.top - bounds.height / 2) / bounds.height * 16}deg`);
            event.currentTarget.style.setProperty("--tilt-y", `${(event.clientX - bounds.left - bounds.width / 2) / bounds.width * 16}deg`);
          }}
          onPointerLeave={(event) => { event.currentTarget.style.removeProperty("--tilt-x"); event.currentTarget.style.removeProperty("--tilt-y"); }}
          aria-label={`Abrir ficha destacada de ${featured.title}`} to={`/series/${featured.id}`}>
          <span aria-hidden="true">{progress ? <ProgressCover progress={progress} /> : <SeriesCover series={featured.series!} variant="large" />}</span>
        </Link>
      </div> : <div className={`${styles.heroFallback} ${libraryLoading || progressLoading ? styles.skeleton : ""}`}>
        <p className={styles.eyebrow}>Tu biblioteca de manga</p><h2 id="featured-title">Tu próxima historia <span>empieza aquí.</span></h2>
        <p>Encuentra una historia o abre una de tu biblioteca.</p><Link to="/search" className={styles.primaryAction}>Buscar obras</Link>
      </div>}
      {slides.length > 1 ? <div className={styles.heroControls}>
        <div className={styles.heroDots} role="group" aria-label="Seleccionar obra destacada">{slides.map((work, slideIndex) =>
          <button key={work.id} type="button" aria-label={`Ver destacado: ${work.title}`} aria-pressed={index === slideIndex}
            onClick={() => setActiveSlide(slideIndex)}><span /></button>)}</div>
        <button className={styles.squareAction} type="button" aria-label={paused ? "Reanudar destacados" : "Pausar destacados"} aria-pressed={paused} onClick={() => setPaused(!paused)}><ActionIcon name={paused ? "play" : "pause"} /></button>
        <button className={styles.squareAction} type="button" aria-label="Destacado anterior" onClick={() => setActiveSlide((index + slides.length - 1) % slides.length)}><ActionIcon name="previous" /></button>
        <button className={styles.squareAction} type="button" aria-label="Destacado siguiente" onClick={() => setActiveSlide((index + 1) % slides.length)}><ActionIcon name="next" /></button>
      </div> : null}
    </section>

    <OnlineRecommendations kind="popular" enabled={serviceReady} />
    <section aria-labelledby="recent-library-title" className={`${styles.recentSection} ${styles.recentLibrary}`} data-motion-reveal>
      <header className={styles.sectionHeader}><div><h2 id="recent-library-title"><span className={styles.sectionNumber} aria-hidden="true" />Mi biblioteca</h2><p>Tus últimas obras añadidas, siempre a mano.</p></div><Link className={styles.libraryLink} to="/library">Abrir biblioteca<ActionIcon name="next" /></Link></header>
      {libraryLoading ? <LoadingCards label="Cargando obras recientes…" /> : null}
      {recentLibrary.isError ? <RecentError error={recentLibrary.error} label="Error en biblioteca reciente" titleId="recent-library-error-title"
        onRetry={() => void recentLibrary.refetch()} retryLabel="Reintentar biblioteca reciente" retryDisabled={recentLibrary.isFetching} /> : null}
      {!serviceReady && !recentWorks.length && !libraryLoading ? <p className={styles.statusMessage} role="status">La biblioteca reciente estará disponible cuando el servicio local esté listo.</p> : null}
      {serviceReady && recentLibrary.isSuccess && !recentWorks.length ? <div className={styles.emptyState}><ActionIcon name="grid" /><p>Todavía no hay obras recientes en tu biblioteca.</p><Link className={styles.secondaryAction} to="/search">Buscar obras</Link></div> : null}
      {recentWorks.length ? <ul className={styles.cardRow}>{recentWorks.map((series) => <WorkCard key={series.seriesId} series={series} />)}</ul> : null}
    </section>
  </div>;
}

function formatSavedAt(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : new Intl.DateTimeFormat("es", { dateStyle: "medium", timeStyle: "short" }).format(parsed);
}
