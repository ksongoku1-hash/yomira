import {useQuery} from "@tanstack/react-query";
import {useState} from "react";
import {getPublicSources} from "../api/client";
import {ActionIcon} from "../app/ActionIcon";
import styles from "./AvailableSources.module.css";

export function AvailableSources() {
  const [open,setOpen]=useState(false);
  const sources=useQuery({queryKey:["public-sources"],queryFn:getPublicSources,enabled:open,staleTime:24*60*60*1000});
  return <details className={styles.panel} onToggle={(event)=>setOpen(event.currentTarget.open)}>
    <summary><ActionIcon name="grid"/>Fuentes habilitadas{sources.data ? ` · ${sources.data.length}` : ""}</summary>
    {open ? <div className={styles.content}>
      <p>Estos sitios están integrados en la búsqueda. Los resultados confirman cuáles responden para cada obra.</p>
      {sources.isPending ? <p role="status">Consultando fuentes…</p> : null}
      {sources.isError ? <div role="status"><p>No se pudo consultar la lista. La búsqueda sigue disponible.</p><button type="button" disabled={sources.isFetching} onClick={()=>void sources.refetch()}>Reintentar fuentes</button></div> : null}
      {sources.data ? <ul>{sources.data.map((source)=><li key={source.sourceId}><ActionIcon name="check"/><span>{source.name}</span><small>{source.language==="es"?"Español":"Inglés"}</small></li>)}</ul> : null}
    </div> : null}
  </details>;
}
