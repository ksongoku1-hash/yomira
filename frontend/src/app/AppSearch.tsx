import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { getRecentLibrary } from "../api/client";
import { ActionIcon } from "./ActionIcon";
import styles from "../App.module.css";

export function AppSearch({ ready }: { ready: boolean }) {
  const navigate = useNavigate();
  const searchRef = useRef<HTMLInputElement>(null);
  const [search, setSearch] = useState("");
  const library = useQuery({ queryKey: ["library", "recent", 4], queryFn: () => getRecentLibrary(4), enabled: ready });
  const works = library.data?.data.slice(0, 4) ?? [];
  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey ||
        (event.target instanceof Element && event.target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false'])"))) return;
      event.preventDefault(); searchRef.current?.focus();
    };
    window.addEventListener("keydown", focusSearch);
    return () => window.removeEventListener("keydown", focusSearch);
  }, []);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const query = search.trim();
    if (query && query.length <= 200) navigate(`/search?q=${encodeURIComponent(query)}`);
  };
  return <div className={styles.searchBar}>
    <form className={styles.quickSearch} role="search" aria-label="Buscar historias" onSubmit={submit}>
      <ActionIcon name="search" />
      <input ref={searchRef} aria-label="Buscar una historia" placeholder="Buscar historias…" enterKeyHint="search"
        maxLength={200} value={search} onChange={(event) => setSearch(event.target.value)} required />
      <kbd aria-hidden="true">/</kbd>
      <button className={styles.searchSubmit} type="submit" aria-label="Buscar historias" title="Buscar historias"><ActionIcon name="next" /></button>
    </form>
    <button className={styles.surpriseButton} type="button" aria-label="Sorpréndeme" title="Explorar una obra reciente de tu biblioteca"
      disabled={!ready || library.isError || works.length === 0} onClick={() => {
        const work = works[Math.floor(Math.random() * works.length)];
        if (work) navigate(`/series/${work.seriesId}`);
      }}><ActionIcon name="sparkle" /><span>Sorpréndeme</span></button>
  </div>;
}
