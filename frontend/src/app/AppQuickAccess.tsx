import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { getLibraryPage, getRecentReaderProgress, getSeriesCoverUrl, getSystemReadiness } from "../api/client";
import { ActionIcon } from "./ActionIcon";
import { useAppearanceStore } from "./appearanceStore";
import styles from "../App.module.css";

const normalize = (value: string) => value.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
export function AppQuickAccess({ open, onOpen, onClose, destinations }: {
  open: boolean; onOpen: () => void; onClose: () => void;
  destinations: readonly { label: string; path: string }[];
}) {
  const navigate = useNavigate();
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const id = useId();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [selected, setSelected] = useState(0);
  const theme = useAppearanceStore((state) => state.theme);
  const readiness = useQuery({ queryKey: ["system", "readiness"], queryFn: getSystemReadiness });
  const ready = readiness.data?.status === "ready";
  const progress = useQuery({ queryKey: ["reader", "progress", "recent", 4, "series"],
    queryFn: () => getRecentReaderProgress(4, true), enabled: ready,
    staleTime: 30_000, refetchOnMount: true, refetchOnWindowFocus: true });
  const resume = progress.data?.data.find((item) => Number(item.percentage) < 100);
  const term = query.trim();
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(term), 180);
    return () => clearTimeout(timer);
  }, [term]);
  const library = useQuery({ queryKey: ["library", "commands", debounced],
    queryFn: () => getLibraryPage({ query: debounced, limit: 6, offset: 0, sort: "title" }),
    enabled: open && ready && !!debounced && debounced === term, retry: false });
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.altKey || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "k") return;
      event.preventDefault(); if (open) onClose(); else onOpen();
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, [open, onOpen, onClose]);
  useEffect(() => {
    if (!open || !dialog.current) return;
    const element = dialog.current;
    const previous = document.activeElement;
    setQuery(""); setDebounced(""); setSelected(0);
    element.showModal(); input.current?.focus();
    return () => { element.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, [open]);
  const commands = [
    ...destinations.map((item) => ({ label: `Ir a ${item.label}`, detail: "Pantalla", run: () => navigate(item.path) })),
    ...(resume ? [{ label: `Continuar ${resume.seriesDisplayTitle}`, detail: `Página ${resume.pageIndex + 1}`,
      run: () => navigate(`/reader/${resume.chapterId}`, { state: { originatingSeriesId: resume.seriesId } }) }] : []),
    { label: `Cambiar al tema ${theme === "tinta" ? "Papel" : "Tinta"}`, detail: "Apariencia",
      run: () => useAppearanceStore.getState().update({ theme: theme === "tinta" ? "papel" : "tinta" }) },
  ].filter((item) => normalize(item.label).includes(normalize(term)));
  const works = term && term === debounced ? library.data?.data ?? [] : [];
  const items = [
    ...commands,
    ...works.map((work) => ({ label: work.displayTitle, detail: "En tu biblioteca", run: () => navigate(`/series/${work.seriesId}`) })),
    ...(term ? [{ label: `Buscar «${term}» en las fuentes`, detail: "Explorar", run: () => navigate(`/search?q=${encodeURIComponent(term)}`) }] : []),
  ];
  const active = Math.min(selected, Math.max(0, items.length - 1));
  useEffect(() => {
    if (open) list.current?.children.item(active)?.scrollIntoView?.({ block: "nearest" });
  }, [open, active, items.length]);
  function run(index: number) { const item = items[index]; if (item) { onClose(); item.run(); } }
  return <>
    <div className={styles.quickAccess}>
      <button className={styles.commandButton} type="button" aria-label="Abrir paleta de comandos" title="Comandos (Ctrl + K)" onClick={onOpen}>
        <ActionIcon name="keyboard" /><span className={styles.navigationLabel}>Comandos</span><kbd>Ctrl K</kbd>
      </button>
      {resume ? <Link className={styles.miniReading} aria-label={`Continuar leyendo ${resume.seriesDisplayTitle}`}
        title={`${resume.seriesDisplayTitle} · ${resume.chapterDisplayLabel} · Página ${resume.pageIndex + 1}`}
        to={`/reader/${resume.chapterId}`} state={{ originatingSeriesId: resume.seriesId }}>
        <span className={styles.miniReadingCover}><ActionIcon name="page" />
          <img src={getSeriesCoverUrl(resume.seriesId, "small", resume.updatedAt)} alt="" width="36" height="54"
            onError={(event) => { event.currentTarget.hidden = true; }} onLoad={(event) => { event.currentTarget.hidden = false; }} />
        </span>
        <span className={styles.miniReadingCopy}><small>Continuar</small><strong>{resume.seriesDisplayTitle}</strong>
          <progress value={Number(resume.percentage)} max={100} aria-label={`Progreso de ${resume.seriesDisplayTitle}`} /></span>
      </Link> : null}
    </div>
    <dialog ref={dialog} className={styles.commandDialog} aria-labelledby={`${id}-title`} onCancel={onClose} onClose={onClose}
      onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className={styles.commandContent}>
        <header><h2 id={`${id}-title`}>Ir a una obra o pantalla</h2><button type="button" aria-label="Cerrar comandos" onClick={onClose}><ActionIcon name="close" /></button></header>
        <label className={styles.commandInput}><ActionIcon name="search" />
          <input ref={input} role="combobox" aria-label="Comando" aria-autocomplete="list" aria-expanded={open}
            aria-controls={`${id}-list`} aria-activedescendant={items.length ? `${id}-${active}` : undefined}
            value={query} maxLength={200} placeholder="Busca una obra o una pantalla…" autoComplete="off"
            onChange={(event) => { setQuery(event.target.value); setSelected(0); }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault(); setSelected((active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % Math.max(1, items.length));
              } else if (event.key === "Home") { event.preventDefault(); setSelected(0); }
              else if (event.key === "End") { event.preventDefault(); setSelected(Math.max(0, items.length - 1)); }
              else if (event.key === "Enter" && !event.nativeEvent.isComposing) { event.preventDefault(); run(active); }
            }} />
        </label>
        <ul ref={list} id={`${id}-list`} role="listbox" aria-label="Obras y acciones">{items.map((item, index) =>
          <li key={`${item.detail}:${item.label}`} id={`${id}-${index}`} role="option" aria-selected={active === index}
            onMouseDown={(event) => event.preventDefault()} onClick={() => run(index)}>
            <span>{item.label}</span><small>{item.detail}</small><ActionIcon name="next" />
          </li>)}</ul>
        {term && (debounced !== term || library.isFetching) ? <p role="status">Buscando en tu biblioteca…</p> : null}
        {term && debounced === term && library.isError ? <p role="alert">No se pudo buscar en tu biblioteca. <button type="button" onClick={() => void library.refetch()}>Reintentar búsqueda</button></p> : null}
        <footer><span>↑ ↓ Elegir · Intro Abrir</span><span>Esc Cerrar</span></footer>
      </div>
    </dialog>
  </>;
}
