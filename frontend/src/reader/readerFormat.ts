import type { ReaderWorkPreferences } from "./readerLibrary";

export interface DetectedReaderFormat {
  label: string;
  reason: string;
  preferences: ReaderWorkPreferences;
}

export function formatPreferences(format: "manga" | "manhwa" | "comic"): ReaderWorkPreferences {
  return { format, mode: format === "manhwa" ? "vertical" : "single", rtl: format === "manga", continuous: format === "manhwa" };
}

export function detectReaderFormat(genres: readonly string[], pageRatios: readonly number[]): DetectedReaderFormat | null {
  const tags = new Set(genres.map((genre) => genre.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase().replace(/[^a-z]/g, "")));
  const strip = ["webtoon", "longstrip", "tiravertical"].some((tag) => tags.has(tag));
  const tallPage = pageRatios.some((ratio) => Number.isFinite(ratio) && ratio > 0 && ratio <= 0.2);
  if (strip || tallPage) return {
    label: tags.has("manhwa") || tags.has("manwha") ? "Manhwa · tira vertical" : "Tira vertical",
    reason: strip ? "La ficha indica un formato de tira vertical." : "Se detectó una página al menos cinco veces más alta que ancha.",
    preferences: { format: "auto", mode: "vertical", rtl: false, continuous: true },
  };
  const types = (["manga", "manhwa", "comic"] as const).filter((format) =>
    tags.has(format) || (format === "manhwa" && tags.has("manwha")) || (format === "comic" && tags.has("comics")));
  if (types.length !== 1) return null;
  const format = types[0]!;
  return { label: { manga: "Manga", manhwa: "Manhwa", comic: "Cómic" }[format],
    reason: "Tipo indicado en los metadatos de la obra.", preferences: { ...formatPreferences(format), format: "auto" } };
}
