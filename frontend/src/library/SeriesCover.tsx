import { useState } from "react";

import { getSeriesCoverUrl } from "../api/client";
import type { LibrarySeriesSummary, SeriesCoverVariant } from "../api/client";
import styles from "./SeriesCover.module.css";

export function SeriesCover({
  series,
  variant = "medium",
  className = "",
  fallbackLabel = "Sin portada asignada",
  previewSource = null,
  retryKey = 0,
}: {
  series: LibrarySeriesSummary;
  variant?: SeriesCoverVariant;
  className?: string | undefined;
  fallbackLabel?: string;
  previewSource?: string | null;
  retryKey?: number;
}) {
  const [failedSource, setFailedSource] = useState<string | null>(null);
  const source = series.coverAssetId === null
    ? previewSource
    : getSeriesCoverUrl(series.seriesId, variant, `${series.coverAssetId}:${series.updatedAt}${retryKey ? `:${retryKey}` : ""}`);

  if (source !== null && source !== failedSource) {
    return (
      <img
        alt={`Portada de ${series.displayTitle}`}
        className={`${styles.cover} ${className}`}
        decoding="async"
        loading="lazy"
        onError={() => setFailedSource(source)}
        src={source}
      />
    );
  }

  const label = source === null ? fallbackLabel : "Vista previa no disponible";
  return (
    <div
      aria-label={`${label}: ${series.displayTitle}`}
      className={`${styles.cover} ${styles.placeholder} ${className}`}
      role="img"
    >
      <span aria-hidden="true" className={styles.mark} />
      <span>{label}</span>
    </div>
  );
}
