import { useState } from "react";

import { useAppearanceStore } from "../app/appearanceStore";
import styles from "./IllustratedBackdrop.module.css";

/** Decorative layer: the parent provides positioning and an isolated stacking context. */
export function IllustratedBackdrop({ source }: { source: string | null }) {
  const enabled = useAppearanceStore((state) => state.enabled);
  const intensity = useAppearanceStore((state) => state.intensity);
  const simpleGraphics = useAppearanceStore((state) => state.simpleGraphics);
  const [loadedSource, setLoadedSource] = useState<string | null>(null);
  const [failedSource, setFailedSource] = useState<string | null>(null);

  if (!enabled || simpleGraphics || intensity === 0 || source === null || source === failedSource) return null;

  return (
    <div aria-hidden="true" className={styles.backdrop}>
      <img
        alt=""
        className={styles.illustration}
        decoding="async"
        fetchPriority="low"
        loading="lazy"
        onError={() => setFailedSource(source)}
        onLoad={() => setLoadedSource(source)}
        src={source}
        style={{ opacity: loadedSource === source ? intensity / 100 : 0 }}
      />
      <div className={styles.shade} />
    </div>
  );
}
