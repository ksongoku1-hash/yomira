import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { getRecommendations, type Recommendation, type RecommendationProvider } from "../api/client";
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
const CATALOGS: Record<RecommendationProvider, {url: string; label: string}> = {
  MangaDex: {url:"https://mangadex.org",label:"MangaDex · español"},
  AniList: {url:"https://anilist.co/search/manga/trending",label:"AniList · tendencias"},
  MyAnimeList: {url:"https://myanimelist.net/topmanga.php?type=bypopularity",label:"MyAnimeList · Jikan"},
};
function Cover({item}: {item: Recommendation}) {
  const [failed, setFailed] = useState(false);
  return item.coverUrl && !failed ? <img src={item.coverUrl.startsWith("https://uploads.mangadex.org/") ? `${item.coverUrl}.256.jpg` : item.coverUrl} alt={`Portada de ${item.title}`}
    className={styles.recentCover} loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={()=>setFailed(true)} />
    : <div className={styles.coverPlaceholder} role="img" aria-label={`Portada no disponible: ${item.title}`}><ActionIcon name="page" /></div>;
}

export function OnlineRecommendations({kind, enabled = true, readingTitles = [], readingGenres = []}: {
  kind: "popular" | "personal"; enabled?: boolean; readingTitles?: string[]; readingGenres?: string[];
}) {
  const [genre, setGenre] = useState("");
  const [provider, setProvider] = useState<RecommendationProvider>("MangaDex");
  const [limit, setLimit] = useState(kind === "personal" ? 8 : 12);
  const query = useQuery({queryKey:["recommendations",provider], queryFn:()=>getRecommendations(provider), enabled,
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
  const items = candidates.filter(({item})=>!genre || item.genres.some((name)=>genreLabel(name)===genre)).slice(0,limit);
  const meta = query.data?.meta;
  return <section className={styles.recentSection} data-motion-reveal aria-labelledby={`recommendations-${kind}-title`}>
    <header className={styles.sectionHeader}><div><h2 id={`recommendations-${kind}-title`}><span className={styles.sectionNumber} aria-hidden="true" /><ActionIcon name={personal?"sparkle":"flame"} />{title}</h2>
      <p>{personal ? (likes.size ? "Según los géneros de tus lecturas recientes. La disponibilidad se comprueba al buscar." : "Elige un género o empieza por estas lecturas populares. La disponibilidad se comprueba al buscar.") : provider === "MangaDex" ? "Por seguidores en MangaDex, con catálogo en español. Busca un título para ver nuestras fuentes." : `${provider === "AniList" ? "Tendencias" : "Popularidad"} en ${provider}. Comprueba las ediciones en español o inglés al buscar fuentes.`}</p></div>
      <div className={styles.discoveryActions}>
        <label><span className={styles.srOnly}>Catálogo de {title}</span><select className={styles.secondaryAction} aria-label={`Catálogo de ${title}`} value={provider}
          onChange={(event)=>{setProvider(event.target.value as RecommendationProvider);setGenre("");setLimit(personal?8:12);}}>
          {Object.entries(CATALOGS).map(([value,catalog])=><option key={value} value={value}>{catalog.label}</option>)}
        </select></label>
        <a className={styles.libraryLink} href={CATALOGS[provider].url} target="_blank" rel="noopener noreferrer">{provider}<ActionIcon name="external" /></a>
      </div></header>
    {meta?.updatedAt ? <p className={styles.discoveryStatus}>{meta.status==="stale" ? "Sin conexión con la fuente · última lista guardada: " : "Lista consultada: "}
      <time dateTime={meta.updatedAt}>{new Intl.DateTimeFormat("es",{dateStyle:"medium",timeStyle:"short"}).format(new Date(meta.updatedAt))}</time></p> : null}
    {!enabled && !query.data ? <p className={styles.statusMessage}>Las recomendaciones estarán disponibles cuando el servicio local esté listo.</p> : null}
    {enabled && query.isPending ? <div role="status" aria-label="Cargando recomendaciones online" className={`${styles.cardRow} ${styles.shelf}`}>
      {Array.from({length:4},(_,index)=><div key={index} aria-hidden="true" className={styles.skeletonCard}><div /><span /><span /></div>)}
      <span className={styles.srOnly}>Cargando recomendaciones online…</span></div> : null}
    {query.isError || meta?.status==="unavailable" ? <div className={styles.emptyState} role="status"><ActionIcon name="refresh" /><div>
      <h3>No se pudieron consultar las recomendaciones</h3><p>Tu biblioteca y el progreso siguen disponibles. Puedes buscar una obra directamente.</p>
      {meta?.retryAt ? <p>La fuente volverá a consultarse después de {new Intl.DateTimeFormat("es",{timeStyle:"short"}).format(new Date(meta.retryAt))}.</p> : null}
      </div><button className={styles.secondaryAction} type="button" disabled={query.isFetching} onClick={()=>void query.refetch()}>Comprobar recomendaciones</button></div> : null}
    {personal && genres.length ? <div className={styles.progressFilters} role="group" aria-label="Géneros de recomendaciones online">
      {["",...genres].map((value)=><button key={value} type="button" aria-pressed={value===genre} onClick={()=>{setGenre(value);setLimit(personal?8:12);}}>{value||"Todos"}</button>)}</div> : null}
    {items.length ? <ul className={`${styles.cardRow} ${styles.shelf}`}>{items.map(({item,rank,match})=><li data-motion-card className={styles.workItem} key={item.remoteId}><article>
      <div className={styles.workCover}><Link className={styles.seriesLink} to={`/search?q=${encodeURIComponent(item.title)}`} aria-label={`Buscar fuentes de ${item.title}`}>
        <Cover item={item} /></Link><span className={styles.discoveryRank}>#{rank}</span></div>
      <Link className={styles.itemTitle} title={item.title} to={`/search?q=${encodeURIComponent(item.title)}`}>{item.title}</Link>
      <span className={styles.itemMetadata}>{item.genres.slice(0,2).map(genreLabel).join(" · ") || `Popular en ${provider}`}</span>
      {personal ? <span className={styles.recommendationReason}>{match ? `Porque lees ${genreLabel(match)}` : `Popular en ${provider}`}</span> : null}
      <Link className={`${styles.primaryAction} ${styles.findSources}`} to={`/search?q=${encodeURIComponent(item.title)}`}><ActionIcon name="search" />Buscar fuentes</Link>
      {item.aliases.length ? <details className={styles.alternativeTitles}><summary>Otros nombres</summary>{item.aliases.map((alias)=><Link key={alias} to={`/search?q=${encodeURIComponent(alias)}`}>{alias}</Link>)}</details> : null}
    </article></li>)}</ul> : null}
    {candidates.filter(({item})=>!genre || item.genres.some((name)=>genreLabel(name)===genre)).length > limit ?
      <button className={styles.secondaryAction} type="button" onClick={()=>setLimit((value)=>value+8)}>Ver más recomendaciones</button> : null}
    {!query.isPending && !query.isError && meta?.status!=="unavailable" && !items.length ? <div className={styles.emptyState}><ActionIcon name="sparkle" />
      <p>{genre ? `No hay títulos de ${genre} en esta lista.` : "No hay recomendaciones nuevas en esta lista."}</p><Link className={styles.secondaryAction} to="/search">Explorar historias</Link></div> : null}
  </section>;
}
