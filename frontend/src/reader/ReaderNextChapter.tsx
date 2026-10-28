import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import type { ReaderChapter } from "../api/client";
import type { OnlineChapterTarget } from "./ReaderPage";
import styles from "./ReaderPage.module.css";

const DURATION = 5000;
export function ReaderNextChapter({ next, automatic, disabled, onOpen, onCancel }: {
  next: ReaderChapter | OnlineChapterTarget;
  automatic: boolean;
  disabled: boolean;
  onOpen: (next: ReaderChapter | OnlineChapterTarget) => void;
  onCancel: () => void;
}) {
  const [elapsed, setElapsed] = useState(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  function stop() {
    if (timer.current !== null) clearInterval(timer.current);
    timer.current = null;
  }
  // Keep countdown frames out of the page/image rendering tree.
  useEffect(() => {
    if (!automatic) return;
    setElapsed(0);
    const started = Date.now();
    timer.current = setInterval(() => {
      const time = Math.min(DURATION, Date.now() - started);
      setElapsed(time);
      if (time >= DURATION) { stop(); onOpen(next); }
    }, 100);
    return stop;
  }, [automatic, next, onOpen]);
  const seconds = Math.ceil((DURATION - elapsed) / 1000);
  const className = `${styles.primaryButton} ${automatic ? styles.nextChapterCountdown : ""}`;
  const style = { "--advance-progress": elapsed / DURATION } as CSSProperties;
  const label = <span className={styles.nextChapterLabel}>Siguiente capítulo · {next.displayLabel}{automatic ? ` · ${seconds} s` : ""}</span>;
  return <>
    {"chapterId" in next
      ? <Link className={className} style={style} to={`/reader/${next.chapterId}`} state={{ originatingSeriesId: next.seriesId }} onClick={stop}>{label}</Link>
      : <button className={className} style={style} type="button" disabled={disabled} onClick={() => { stop(); onOpen(next); }}>{label}</button>}
    {automatic ? <span>
      <span role="status">El siguiente capítulo se abrirá en {seconds} segundos.</span>{" "}
      <button type="button" className={styles.secondaryButton} onClick={() => { stop(); onCancel(); }}>Cancelar avance automático</button>
    </span> : null}
  </>;
}
