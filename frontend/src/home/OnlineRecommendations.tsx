import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { getRecommendations, type Recommendation } from "../api/client";
import { ActionIcon } from "../app/ActionIcon";
import styles from "./HomePage.module.css";

const GENRES: Record<string, string> = {action:"Acción", adventure:"Aventura", comedy:"Comedia", drama:"Drama",
  fantasy:"Fantasía", horror:"Terror", mystery:"Misterio", romance:"Romance", scifi:"Ciencia ficción",
  sports:"Deporte", martialarts:"Artes marciales", sliceoflife:"Vida cotidiana", psychological:"Psicológico",
  awardwinning:"Premiado", monsters:"Monstruos", reincarnation:"Reencarnación", longstrip:"Webtoon", magic:"Magia",
  demons:"Demonios", thriller:"Suspenso", schoollife:"Vida escolar", supernatural:"Sobrenatural", fullcolor:"A color"};
const normalized = (value: string) => value.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
function genreKey(value: string) { const key=normalized(value); return Object.keys(GENRES).find((name)=>name===key || normalized(GENRES[name]!)===key) ?? key; }
function genreLabel(value: string) { return GENRES[genreKey(value)] ?? value; }
function Cover({item}: {item: Recommendation}) {
  const [failed, setFailed] = useState(false);
  return item.coverUrl && !failed ? <img src={`${item.coverUrl}.256.jpg`} alt={`Portada de ${item.title}`}
    className={styles.recentCover} loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={()=>setFailed(true)} />
    : <div className={styles.coverPlaceholder} role="img" aria-label={`Portada no disponible: ${item.title}`}><ActionIcon name="page" /></div>;
}

export function OnlineRecommendations({kind, enabled = true, readingTitles = [], readingGenres = []}: {
  kind: "popular" | "personal"; enabled?: boolean; readingTitles?: string[]; readingGenres?: string[];
}) {
  const [genre, setGenre] = useState("");
  const query = useQuery({queryKey:["recommendations"], queryFn:getRecommendations, enabled,
    refetchOnWindowFocus:true, staleTime:(state)=>state.state.data?.meta.status==="ready" && state.state.data.meta.updatedAt
      ? Math.max(0,Date.parse(state.state.data.meta.updatedAt)+24*60*60*1000-Date.now()) : 60_000});
  const personal = kind === "personal";
  const title = personal ? "Descubre algo nuevo" : "Populares online";
  const seen = new Set(readingTitles.map(normalized));
  const likes = new Set(readingGenres.map(genreKey));
  const ranked = (query.data?.data ?? []).map((item,index)=>({item,rank:index+1,
    match:item.genres.find((name)=>likes.has(genreKey(name)))}));
  const candidates = personal ? ranked.filter(({item})=>![item.title,...item.aliases].some((name)=>seen.has(normalized(name))))
    .sort((a,b)=>Number(!!b.match)-Number(!!a.match) || a.rank-b.rank) : ranked;
  const genres = [...new Set(candidates.flatMap(({item})=>item.genres.map(genreLabel)))].slice(0,8);
  const items = candidates.filter(({item})=>!genre || item.genres.some((name)=>genreLabel(name)===genre)).slice(0,personal?8:12);
  const meta = query.data?.meta;
  return <section className={styles.recentSection} aria-labelledby={`recommendations-${kind}-title`}>
    <header className={styles.sectionHeader}><div><h2 id={`recommendations-${kind}-title`}><ActionIcon name={personal?"sparkle":"flame"} />{title}</h2>
      <p>{personal ? (likes.size ? "Según los géneros de tus lecturas recientes. La disponibilidad se comprueba al buscar." : "Elige un género o empieza por estas lecturas populares. La disponibilidad se comprueba al buscar.") : "Por seguidores en MangaDex, con catálogo en español. Busca un título para ver nuestras fuentes."}</p></div>
      <a className={styles.libraryLink} href="https://mangadex.org" target="_blank" rel="noopener noreferrer">MangaDex<ActionIcon name="external" /></a></header>
    {meta?.updatedAt ? <p className={styles.discoveryStatus}>{meta.status==="stale" ? "Sin conexión con la fuente · última lista guardada: " : "Lista consultada: "}
      <time dateTime={meta.updatedAt}>{new Intl.DateTimeFormat("es",{dateStyle:"medium",timeStyle:"short"}).format(new Date(meta.updatedAt))}</time></p> : null}
    {!enabled && !query.data ? <p className={styles.statusMessage}>Las recomendaciones estarán disponibles cuando el servicio local esté listo.</p> : null}
    {enabled && query.isPending ? <div role="status" aria-label="Cargando recomendaciones online" className={styles.cardRow}>
      {Array.from({length:4},(_,index)=><div key={index} aria-hidden="true" className={styles.skeletonCard}><div /><span /><span /></div>)}
      <span className={styles.srOnly}>Cargando recomendaciones online…</span></div> : null}
    {query.isError || meta?.status==="unavailable" ? <div className={styles.emptyState} role="status"><ActionIcon name="refresh" /><div>
      <h3>No se pudieron consultar las recomendaciones</h3><p>Tu biblioteca y el progreso siguen disponibles. Puedes buscar una obra directamente.</p>
      {meta?.retryAt ? <p>La fuente volverá a consultarse después de {new Intl.DateTimeFormat("es",{timeStyle:"short"}).format(new Date(meta.retryAt))}.</p> : null}
      </div><button className={styles.secondaryAction} type="button" disabled={query.isFetching} onClick={()=>void query.refetch()}>Comprobar recomendaciones</button></div> : null}
    {personal && genres.length ? <div className={styles.progressFilters} role="group" aria-label="Géneros de recomendaciones online">
      {["",...genres].map((value)=><button key={value} type="button" aria-pressed={value===genre} onClick={()=>setGenre(value)}>{value||"Todos"}</button>)}</div> : null}
    {items.length ? <ul className={styles.cardRow}>{items.map(({item,rank,match})=><li className={styles.workItem} key={item.remoteId}><article>
      <div className={styles.workCover}><Link className={styles.seriesLink} to={`/search?q=${encodeURIComponent(item.title)}`} aria-label={`Buscar fuentes de ${item.title}`}>
        <Cover item={item} /></Link><span className={styles.discoveryRank}>#{rank} · MangaDex</span></div>
      <Link className={styles.itemTitle} title={item.title} to={`/search?q=${encodeURIComponent(item.title)}`}>{item.title}</Link>
      <span className={styles.itemMetadata}>{item.genres.slice(0,2).map(genreLabel).join(" · ") || "Popular en MangaDex"}</span>
      {personal ? <span className={styles.recommendationReason}>{match ? `Porque lees ${genreLabel(match)}` : "Popular en MangaDex"}</span> : null}
      <Link className={`${styles.primaryAction} ${styles.findSources}`} to={`/search?q=${encodeURIComponent(item.title)}`}><ActionIcon name="search" />Buscar fuentes</Link>
      {item.aliases.length ? <details className={styles.alternativeTitles}><summary>Otros nombres</summary>{item.aliases.map((alias)=><Link key={alias} to={`/search?q=${encodeURIComponent(alias)}`}>{alias}</Link>)}</details> : null}
    </article></li>)}</ul> : null}
    {!query.isPending && !query.isError && meta?.status!=="unavailable" && !items.length ? <div className={styles.emptyState}><ActionIcon name="sparkle" />
      <p>{genre ? `No hay títulos de ${genre} en esta lista.` : "No hay recomendaciones nuevas en esta lista."}</p><Link className={styles.secondaryAction} to="/search">Explorar historias</Link></div> : null}
  </section>;
}
