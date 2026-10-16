export interface ReaderPreferences {
  visibility: "auto" | "always" | "manual";
  immersive: boolean;
  progressBar: boolean;
  compact: boolean;
  shortcut: "h" | "c";
  zoom: number;
  fit: "width" | "height" | null;
  continuous: boolean;
  autoChapter: boolean;
}

export const READER_PREFERENCES_KEY = "yomira.reader.presentation.v1";
const defaults: ReaderPreferences = {
  visibility: "always", immersive: false, progressBar: true, compact: true,
  shortcut: "h", zoom: 1, fit: null, continuous: false, autoChapter: false,
};

export function readReaderPreferences(): ReaderPreferences {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(READER_PREFERENCES_KEY) ?? "null");
    if (typeof value !== "object" || value === null || Array.isArray(value)) return { ...defaults };
    const saved = value as Partial<ReaderPreferences>;
    return {
      visibility: ["auto", "always", "manual"].includes(saved.visibility ?? "") ? saved.visibility! : defaults.visibility,
      immersive: typeof saved.immersive === "boolean" ? saved.immersive : defaults.immersive,
      progressBar: typeof saved.progressBar === "boolean" ? saved.progressBar : defaults.progressBar,
      compact: typeof saved.compact === "boolean" ? saved.compact : defaults.compact,
      shortcut: saved.shortcut === "c" ? "c" : "h",
      zoom: typeof saved.zoom === "number" && Number.isFinite(saved.zoom) && saved.zoom >= 0.5 && saved.zoom <= 3 ? saved.zoom : 1,
      fit: saved.fit === "width" || saved.fit === "height" ? saved.fit : null,
      continuous: typeof saved.continuous === "boolean" ? saved.continuous : false,
      autoChapter: typeof saved.autoChapter === "boolean" ? saved.autoChapter : false,
    };
  } catch { return { ...defaults }; }
}

export function saveReaderPreferences(value: ReaderPreferences): boolean {
  try { localStorage.setItem(READER_PREFERENCES_KEY, JSON.stringify(value)); return true; }
  catch { return false; }
}
